"""The LLM gateway orchestrator (Vol 7 §8, ADR-007, ADR-INT-03).

One async entrypoint (:meth:`LlmGateway.chat` / ``stream`` / ``embed``) sits in
front of every provider. It resolves the alias→provider routing, dispatches to
the right adapter, and stamps the cross-cutting response fields uniformly —
provider, model_id, latency, and cost from the per-alias price table (§8.4).

Fallback *execution* (TITAN-INT-063) is deliberately absent: this phase resolves
and validates fallback chains but never fails over — that lands in Phase 17.
"""

import time
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx

from titan.llm.adapters import AnthropicAdapter, OllamaAdapter
from titan.llm.adapters.base import ChatAdapter, EmbeddingsAdapter
from titan.llm.config import ConfigLoader, Price
from titan.llm.errors import ProviderNotConfiguredError
from titan.llm.models import (
    ChatRequest,
    ChatResponse,
    EmbeddingsRequest,
    EmbeddingsResponse,
    EmbeddingVector,
    StreamDelta,
    Usage,
)
from titan.llm.routing import ModelRouter, ResolvedModel

_MILLION = Decimal(1_000_000)


def compute_cost(price: Price | None, usage: Usage) -> Decimal:
    """Cost of one call from the alias price table; free (Ollama, unpriced) ⇒ 0."""
    if price is None:
        return Decimal(0)
    return (
        Decimal(usage.input_tokens) / _MILLION * price.input_per_mtok
        + Decimal(usage.output_tokens) / _MILLION * price.output_per_mtok
    )


class LlmGateway:
    """Provider-agnostic gateway over a hot-reloadable config."""

    def __init__(self, loader: ConfigLoader, client: httpx.AsyncClient) -> None:
        self._loader = loader
        self._client = client
        anthropic = AnthropicAdapter()
        ollama = OllamaAdapter()
        self._chat_adapters: dict[str, ChatAdapter] = {
            "anthropic": anthropic,
            "ollama": ollama,
        }
        self._embed_adapters: dict[str, EmbeddingsAdapter] = {"ollama": ollama}

    async def aclose(self) -> None:
        await self._client.aclose()

    def _router(self) -> ModelRouter:
        # Rebuilt per request so a hot-reloaded models.yaml takes effect at once.
        return ModelRouter(self._loader.get())

    def _chat_adapter(self, resolved: ResolvedModel) -> ChatAdapter:
        adapter = self._chat_adapters.get(resolved.provider)
        if adapter is None:
            raise ProviderNotConfiguredError(
                f"No chat adapter for provider '{resolved.provider}' "
                f"(alias '{resolved.alias}'); OpenAI/Google land in Phase 16."
            )
        return adapter

    async def chat(self, req: ChatRequest) -> ChatResponse:
        router = self._router()
        resolved = router.resolve_chat(model_alias=req.model_alias, agent_id=req.metadata.agent_id)
        adapter = self._chat_adapter(resolved)
        provider_cfg = router.provider_config(resolved.provider)
        start = time.perf_counter()
        result = await adapter.chat(self._client, req=req, resolved=resolved, provider=provider_cfg)
        latency_ms = int((time.perf_counter() - start) * 1000)
        return ChatResponse(
            text=result.text,
            tool_calls=result.tool_calls,
            finish_reason=result.finish_reason,
            usage=result.usage,
            provider=resolved.provider,
            model_id=resolved.model_id,
            cost_usd=compute_cost(resolved.price, result.usage),
            latency_ms=latency_ms,
        )

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamDelta]:
        router = self._router()
        resolved = router.resolve_chat(model_alias=req.model_alias, agent_id=req.metadata.agent_id)
        adapter = self._chat_adapter(resolved)
        provider_cfg = router.provider_config(resolved.provider)
        async for delta in adapter.stream(
            self._client, req=req, resolved=resolved, provider=provider_cfg
        ):
            yield delta

    async def embed(self, req: EmbeddingsRequest) -> EmbeddingsResponse:
        router = self._router()
        resolved = router.resolve_embeddings(model_alias=req.model_alias)
        adapter = self._embed_adapters.get(resolved.provider)
        if adapter is None:
            raise ProviderNotConfiguredError(
                f"No embeddings adapter for provider '{resolved.provider}' "
                f"(alias '{resolved.alias}')."
            )
        provider_cfg = router.provider_config(resolved.provider)
        start = time.perf_counter()
        result = await adapter.embed(
            self._client, req=req, resolved=resolved, provider=provider_cfg
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        vectors = [EmbeddingVector(index=i, embedding=vec) for i, vec in enumerate(result.vectors)]
        dimensions = len(vectors[0].embedding) if vectors else (resolved.dimensions or 0)
        return EmbeddingsResponse(
            embeddings=vectors,
            dimensions=dimensions,
            provider=resolved.provider,
            model_id=resolved.model_id,
            usage=result.usage,
            cost_usd=compute_cost(resolved.price, result.usage),
            latency_ms=latency_ms,
        )


__all__ = ["LlmGateway", "compute_cost"]
