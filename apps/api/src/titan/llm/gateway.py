"""The LLM gateway orchestrator (Vol 7 §8, ADR-007, TITAN-FND-021/053).

One async entrypoint sits in front of every provider. ``chat`` now: (1) enforces
per-project/per-task budgets pre-request, (2) dispatches resiliently — retry with
jittered backoff, then fall over along the models.yaml fallback chain on the §8.3
trigger set (5xx/timeout after retries, 429, context-overflow → larger-context
only), gated by a per-provider circuit breaker, (3) tags fallback results
``degraded_model`` and records them on the response + audit trail, and (4) meters
every call into ``llm_usage`` + Prometheus and settles the budget counters.

``engine``/``redis`` are optional — without them budgets are disabled and no
usage rows are written (unit tests, zero-config local runs); metrics still fire.
"""

import functools
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import httpx
import structlog
from opentelemetry import trace
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.llm import metrics
from titan.llm import repository as repo
from titan.llm.adapters import AnthropicAdapter, GoogleAdapter, OllamaAdapter, OpenAIAdapter
from titan.llm.adapters.base import ChatAdapter, ChatResult, EmbeddingsAdapter
from titan.llm.budget import BudgetManager, Reserved, estimate_input_tokens
from titan.llm.config import ConfigLoader, GatewayConfig
from titan.llm.errors import (
    AllProvidersDownError,
    BudgetExceededError,
    ProviderNotConfiguredError,
    UpstreamProviderError,
)
from titan.llm.models import (
    ChatRequest,
    ChatResponse,
    EmbeddingsRequest,
    EmbeddingsResponse,
    EmbeddingVector,
    StreamDelta,
    Usage,
)
from titan.llm.pricing import PriceBook, compute_cost
from titan.llm.redact import redact_chat_request, redact_embeddings_request
from titan.llm.resilience import CircuitBreaker, RetryPolicy
from titan.llm.routing import ModelRouter, ResolvedModel
from titan.redaction import RedactionEngine
from titan.redaction import default_engine as _default_redaction_engine

_DEFAULT_PRICES = Path("config/prices.yaml")
_TRACER = trace.get_tracer("titan.llm")
logger = structlog.get_logger("titan.llm")


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _agent_label(agent_id: str | None) -> str:
    """Bucket a per-instance agent id (``agent.<role>.<n>``) to its role so the
    metric label cardinality stays bounded by the agent roster, not instances."""
    if not agent_id:
        return "unknown"
    parts = agent_id.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else agent_id


class LlmGateway:
    """Provider-agnostic gateway with resilience, budgets, and accounting."""

    def __init__(
        self,
        loader: ConfigLoader,
        client: httpx.AsyncClient,
        *,
        pricebook: PriceBook | None = None,
        engine: AsyncEngine | None = None,
        redis: Redis | None = None,
        breaker: CircuitBreaker | None = None,
        retry: RetryPolicy | None = None,
        budget: BudgetManager | None = None,
        redactor: RedactionEngine | None = None,
    ) -> None:
        self._loader = loader
        self._client = client
        self._engine = engine
        # SEC-054 sink (a): every request is redacted before ANY adapter sees
        # it. Defaults to the process-global engine (patterns + entropy always
        # on, inventory once the vault loads) so a gateway built without
        # explicit wiring still redacts — fail closed, the Phase-68 lesson.
        self._redactor = redactor if redactor is not None else _default_redaction_engine()
        self._pricebook = pricebook or PriceBook(_DEFAULT_PRICES)
        self._breaker = breaker or CircuitBreaker()
        self._retry = retry or RetryPolicy()
        self._budget = budget or BudgetManager(redis, engine)
        ollama = OllamaAdapter()
        openai = OpenAIAdapter()
        self._chat_adapters: dict[str, ChatAdapter] = {
            "anthropic": AnthropicAdapter(),
            "openai": openai,
            "google": GoogleAdapter(),
            "ollama": ollama,
        }
        self._embed_adapters: dict[str, EmbeddingsAdapter] = {"ollama": ollama, "openai": openai}

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def config_loader(self) -> ConfigLoader:
        """The live loader this gateway resolves routing from.

        Exposed so the Settings surface writes through the SAME instance the
        gateway reads. A second ``ConfigLoader`` over the same paths would work
        via mtime, but could not be cache-invalidated after a write, so a
        read-back inside one filesystem timestamp tick would return stale
        routing and make a saved change look lost.
        """
        return self._loader

    def _router(self) -> ModelRouter:
        return ModelRouter(self._loader.get())

    # --- pricing helpers ---------------------------------------------------

    def _cost(self, resolved: ResolvedModel, usage: Usage) -> Decimal:
        price = resolved.price or self._pricebook.price_for(resolved.provider, resolved.model_id)
        return compute_cost(price, usage)

    def _estimated_cost(self, resolved: ResolvedModel, req: ChatRequest) -> Decimal:
        # Worst-case estimate: measured prompt tokens + the full max_tokens output.
        est = Usage(input_tokens=estimate_input_tokens(req), output_tokens=req.max_tokens)
        return self._cost(resolved, est)

    # --- chat --------------------------------------------------------------

    async def chat(self, req: ChatRequest) -> ChatResponse:
        req = redact_chat_request(self._redactor, req)
        config = self._loader.get()
        router = ModelRouter(config)
        primary = router.resolve_chat(model_alias=req.model_alias, agent_id=req.metadata.agent_id)
        project_id = _uuid_or_none(req.metadata.project_id)
        task_id = _uuid_or_none(req.metadata.task_id)
        candidates = self._chain(router, primary)

        candidates, reserved, reserved_estimate = await self._apply_budget(
            config, req, primary, candidates, project_id, task_id
        )

        served, result, latency_ms, error, attempts = await self._dispatch(router, candidates, req)
        if served is None or result is None:
            await self._budget.release(reserved, reserved_estimate)  # unwind the reservation
            raise self._terminal_error(error, attempts)

        degraded = served.alias != primary.alias
        cost = self._cost(served, result.usage)
        response = ChatResponse(
            text=result.text,
            tool_calls=result.tool_calls,
            finish_reason=result.finish_reason,
            usage=result.usage,
            provider=served.provider,
            model_id=served.model_id,
            cost_usd=cost,
            latency_ms=latency_ms,
            requested_alias=primary.alias,
            served_alias=served.alias,
            degraded_model=degraded,
        )
        if degraded:
            metrics.LLM_FALLBACKS_TOTAL.labels(primary.alias, served.alias).inc()
        await self._record(
            config, req, primary, served, response, project_id, task_id, reserved, reserved_estimate
        )
        return response

    def _chain(self, router: ModelRouter, primary: ResolvedModel) -> list[ResolvedModel]:
        chain = [primary]
        for alias in router.fallback_chain(primary.alias):
            chain.append(router.resolve(alias))
        return chain

    async def _apply_budget(
        self,
        config: GatewayConfig,
        req: ChatRequest,
        primary: ResolvedModel,
        candidates: list[ResolvedModel],
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
    ) -> tuple[list[ResolvedModel], Reserved, Decimal]:
        """Pre-request budget gate. Hard-stop if held; else atomically reserve the
        estimate. On breach, fall to an affordable local candidate
        (fallback_then_pause) or persist a hold + 402. Returns the (possibly
        narrowed) candidates, the reservation, and the reserved estimate."""
        if self._engine is None or not self._budget.enabled:
            return candidates, [], Decimal(0)
        async with self._engine.connect() as conn:
            held = await self._budget.held_reason(conn, project_id, task_id)
        if held is not None:
            raise BudgetExceededError("hold", f"LLM calls are on budget hold: {held}")

        estimate = self._estimated_cost(primary, req)
        breach, reserved = await self._budget.reserve(config.budgets, project_id, task_id, estimate)
        if breach is None:
            return candidates, reserved, estimate

        policy = config.budgets.action_on_exceeded if config.budgets else None
        local = next((c for c in candidates if c.provider == "ollama"), None)
        if policy == "fallback_then_pause" and local is not None:
            local_estimate = self._estimated_cost(local, req)
            local_breach, local_reserved = await self._budget.reserve(
                config.budgets, project_id, task_id, local_estimate
            )
            if local_breach is None:  # local fits (free or affordable) — serve it
                return [local], local_reserved, local_estimate
        async with self._engine.begin() as conn:
            await self._budget.persist_hold(
                conn, breach, f"estimated call over {breach.scope} cap {breach.cap}"
            )
        raise BudgetExceededError(
            breach.scope, f"{breach.scope} budget cap {breach.cap} would be exceeded"
        )

    async def _dispatch(
        self, router: ModelRouter, candidates: list[ResolvedModel], req: ChatRequest
    ) -> tuple[ResolvedModel | None, ChatResult | None, int, Exception | None, int]:
        """Try candidates in order with retries + circuit breaking. A
        context-overflow failure restricts the rest of the chain to strictly
        larger-context aliases (§8.3)."""
        last_error: Exception | None = None
        min_context: int | None = None
        attempts = 0
        for cand in candidates:
            if min_context is not None and (cand.context_window or 0) <= min_context:
                continue
            adapter = self._chat_adapters.get(cand.provider)
            if adapter is None:
                last_error = ProviderNotConfiguredError(f"no adapter for '{cand.provider}'")
                continue
            # A servable candidate counts toward exhaustion whether we call it or
            # skip it for an open circuit — so a chain of all-unavailable providers
            # is one paused_provider signal, not the single provider's raw error.
            attempts += 1
            if not self._breaker.allow(cand.provider):
                last_error = AllProvidersDownError(f"{cand.provider} circuit is open")
                continue
            provider_cfg = router.provider_config(cand.provider)
            start = time.perf_counter()
            attempt: Callable[[], Awaitable[ChatResult]] = functools.partial(
                adapter.chat, self._client, req=req, resolved=cand, provider=provider_cfg
            )
            try:
                result = await self._retry.run(attempt)
            except UpstreamProviderError as exc:
                if exc.category == "client_error":
                    # A bad request is not a fallback trigger (§8.3) — fail fast;
                    # every provider would reject it identically.
                    return None, None, 0, exc, attempts
                if exc.category != "context_overflow":
                    # A too-large prompt is a request-shaping issue, not provider
                    # ill-health — it must not trip the circuit breaker.
                    self._breaker.record_failure(cand.provider)
                last_error = exc
                if exc.category == "context_overflow":
                    min_context = cand.context_window or 0
                continue
            except ProviderNotConfiguredError as exc:
                last_error = exc  # e.g. missing key — try the next candidate
                continue
            self._breaker.record_success(cand.provider)
            return cand, result, int((time.perf_counter() - start) * 1000), None, attempts
        return None, None, 0, last_error, attempts

    @staticmethod
    def _terminal_error(error: Exception | None, attempts: int) -> Exception:
        """Map an exhausted chain to a caller-facing error. A config error, a bad
        request, or a single provider's failure surfaces as-is; only when
        MULTIPLE candidates were tried and all failed is it one paused_provider
        signal (503, TITAN-FND-053)."""
        if isinstance(error, ProviderNotConfiguredError):
            return error
        if isinstance(error, UpstreamProviderError) and (
            error.category == "client_error" or attempts <= 1
        ):
            return error
        detail = str(error) if error is not None else "no candidate could serve the request"
        return AllProvidersDownError(detail)

    def _price_version(self, served: ResolvedModel) -> int:
        # A per-alias price override isn't from the schedule — stamp 0 so the row
        # isn't misattributed to an unrelated prices.yaml version.
        return 0 if served.price is not None else self._pricebook.table().version

    async def _record(
        self,
        config: GatewayConfig,
        req: ChatRequest,
        primary: ResolvedModel,
        served: ResolvedModel,
        response: ChatResponse,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        reserved: Reserved,
        estimate: Decimal,
    ) -> None:
        metrics.observe_usage(
            provider=served.provider,
            model=served.model_id,
            agent=_agent_label(req.metadata.agent_id),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_tokens=response.usage.cached_tokens,
            cost_usd=float(response.cost_usd),
            latency_seconds=response.latency_ms / 1000.0,
        )
        if self._engine is not None:
            async with self._engine.begin() as conn:
                await repo.insert_usage(
                    conn,
                    project_id=project_id,
                    task_id=task_id,
                    run_id=None,
                    agent_id=req.metadata.agent_id,
                    requested_alias=primary.alias,
                    served_alias=served.alias,
                    provider=served.provider,
                    model=served.model_id,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cached_tokens=response.usage.cached_tokens,
                    cost_usd=response.cost_usd,
                    latency_ms=response.latency_ms,
                    degraded_model=response.degraded_model,
                    price_version=self._price_version(served),
                )
        # Settle AFTER the ledger commit so a Redis hiccup never unwinds the
        # durable usage row (the two stores are decoupled).
        await self._budget.settle(
            config.budgets, project_id, task_id, reserved, estimate, response.cost_usd
        )

    # --- streaming (metered) + embeddings ----------------------------------

    async def stream(
        self, req: ChatRequest, *, run_id: uuid.UUID | None = None
    ) -> AsyncIterator[StreamDelta]:
        """Stream deltas AND meter the call (Vol 7 §8.1: every call's usage is
        recorded). Wraps an ``llm.call`` span (TITAN-BCK-025) carrying provider/
        model/token/cost attributes — never prompt or completion text
        (TITAN-OPS-016) — so the flow renders as one connected trace. The final
        ``done`` delta carries the usage; it is written to ``llm_usage`` (with the
        caller's ``run_id``) and settled against the budget when the stream ends
        — including on early termination (recorded in the ``finally``).

        Streaming uses the single resolved model (no fallback chain); the primary
        route must serve it. ``run_id`` attributes the row to the caller's run.
        """
        req = redact_chat_request(self._redactor, req)
        config = self._loader.get()
        router = ModelRouter(config)
        resolved = router.resolve_chat(model_alias=req.model_alias, agent_id=req.metadata.agent_id)
        adapter = self._chat_adapters.get(resolved.provider)
        if adapter is None:
            raise ProviderNotConfiguredError(f"no adapter for '{resolved.provider}'")
        project_id = _uuid_or_none(req.metadata.project_id)
        task_id = _uuid_or_none(req.metadata.task_id)
        if self._engine is not None and self._budget.enabled:
            async with self._engine.connect() as conn:
                held = await self._budget.held_reason(conn, project_id, task_id)
            if held is not None:
                raise BudgetExceededError("hold", f"LLM calls are on budget hold: {held}")

        provider_cfg = router.provider_config(resolved.provider)
        usage = Usage()
        start = time.perf_counter()
        with _TRACER.start_as_current_span("llm.call") as span:
            span.set_attribute("gen_ai.system", resolved.provider)
            span.set_attribute("gen_ai.request.model", resolved.model_id)
            span.set_attribute("llm.streaming", True)
            try:
                async for delta in adapter.stream(
                    self._client, req=req, resolved=resolved, provider=provider_cfg
                ):
                    if delta.type == "done" and delta.usage is not None:
                        usage = delta.usage
                    yield delta
            finally:
                latency_ms = int((time.perf_counter() - start) * 1000)
                cost = self._cost(resolved, usage)
                span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
                span.set_attribute("gen_ai.usage.cost_usd", float(cost))
                # Metering is best-effort: a transient usage-write failure must never
                # abort a completion the model already streamed successfully.
                try:
                    await self._record_stream(
                        config, req, resolved, usage, cost, latency_ms, project_id, task_id, run_id
                    )
                except Exception:
                    logger.warning("llm.stream_metering_failed", exc_info=True)

    async def _record_stream(
        self,
        config: GatewayConfig,
        req: ChatRequest,
        resolved: ResolvedModel,
        usage: Usage,
        cost: Decimal,
        latency_ms: int,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        run_id: uuid.UUID | None,
    ) -> None:
        metrics.observe_usage(
            provider=resolved.provider,
            model=resolved.model_id,
            agent=_agent_label(req.metadata.agent_id),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cost_usd=float(cost),
            latency_seconds=latency_ms / 1000.0,
        )
        if self._engine is not None:
            async with self._engine.begin() as conn:
                await repo.insert_usage(
                    conn,
                    project_id=project_id,
                    task_id=task_id,
                    run_id=run_id,
                    agent_id=req.metadata.agent_id,
                    requested_alias=resolved.alias,
                    served_alias=resolved.alias,
                    provider=resolved.provider,
                    model=resolved.model_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_tokens=usage.cached_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    degraded_model=False,
                    price_version=self._price_version(resolved),
                )
        # Streaming settles its actual cost post-call (no pre-reservation), like
        # embeddings; for a local (zero-cost) model this is a no-op.
        await self._budget.settle(config.budgets, project_id, task_id, [], Decimal(0), cost)

    async def embed(self, req: EmbeddingsRequest) -> EmbeddingsResponse:
        req = redact_embeddings_request(self._redactor, req)
        config = self._loader.get()
        router = ModelRouter(config)
        resolved = router.resolve_embeddings(model_alias=req.model_alias)
        adapter = self._embed_adapters.get(resolved.provider)
        if adapter is None:
            raise ProviderNotConfiguredError(
                f"No embeddings adapter for provider '{resolved.provider}'."
            )
        project_id = _uuid_or_none(req.metadata.project_id)
        task_id = _uuid_or_none(req.metadata.task_id)
        # Embeddings honor an active hold (no new spend on a paused budget), but
        # are cheap enough to settle post-call rather than reserve pre-call.
        if self._engine is not None and self._budget.enabled:
            async with self._engine.connect() as conn:
                held = await self._budget.held_reason(conn, project_id, task_id)
            if held is not None:
                raise BudgetExceededError("hold", f"LLM calls are on budget hold: {held}")
        provider_cfg = router.provider_config(resolved.provider)
        start = time.perf_counter()
        result = await adapter.embed(
            self._client, req=req, resolved=resolved, provider=provider_cfg
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        vectors = [EmbeddingVector(index=i, embedding=vec) for i, vec in enumerate(result.vectors)]
        dimensions = len(vectors[0].embedding) if vectors else (resolved.dimensions or 0)
        cost = self._cost(resolved, result.usage)
        await self._record_embedding(config, req, resolved, result.usage, cost, latency_ms)
        return EmbeddingsResponse(
            embeddings=vectors,
            dimensions=dimensions,
            provider=resolved.provider,
            model_id=resolved.model_id,
            usage=result.usage,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    async def _record_embedding(
        self,
        config: GatewayConfig,
        req: EmbeddingsRequest,
        resolved: ResolvedModel,
        usage: Usage,
        cost: Decimal,
        latency_ms: int,
    ) -> None:
        metrics.observe_usage(
            provider=resolved.provider,
            model=resolved.model_id,
            agent=_agent_label(req.metadata.agent_id),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cost_usd=float(cost),
            latency_seconds=latency_ms / 1000.0,
        )
        project_id = _uuid_or_none(req.metadata.project_id)
        task_id = _uuid_or_none(req.metadata.task_id)
        if self._engine is not None:
            async with self._engine.begin() as conn:
                await repo.insert_usage(
                    conn,
                    project_id=project_id,
                    task_id=task_id,
                    run_id=None,
                    agent_id=req.metadata.agent_id,
                    requested_alias=resolved.alias,
                    served_alias=resolved.alias,
                    provider=resolved.provider,
                    model=resolved.model_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_tokens=usage.cached_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    degraded_model=False,
                    price_version=self._price_version(resolved),
                )
        # Embeddings settle their (usually zero) cost toward the caps; no
        # reservation was made, so the full actual cost is added.
        await self._budget.settle(config.budgets, project_id, task_id, [], Decimal(0), cost)


__all__ = ["LlmGateway", "compute_cost"]
