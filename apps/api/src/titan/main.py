"""FastAPI application factory for titan-core (TITAN-BCK-001).

``uvicorn titan.main:app`` starts the full API. The §9.1 gateway middleware
stack lives here, in its mandated order; routers and strategies accumulate
phase by phase on this single factory.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.utils import get_openapi

from titan import clients
from titan.agents import router as agents_router
from titan.agents import service as agents_service
from titan.artifacts import router as artifacts_router
from titan.audit import router as audit_router
from titan.audit import service as audit_service
from titan.clarification import router as clarification_router
from titan.constitution import router as constitution_router
from titan.debt import router as debt_router
from titan.deploy import router as deploy_router
from titan.events import service as events_service
from titan.flags import router as flags_router
from titan.flags import service as flags_service
from titan.gateway.idempotency import IdempotencyMiddleware
from titan.gateway.middleware import RequestContextMiddleware
from titan.gateway.openapi import problemify_openapi
from titan.gateway.problems import register_problem_handlers
from titan.gateway.ratelimit import RateLimiter, RateLimitMiddleware, build_route_class_table
from titan.gateway.rbac import KNOWN_SCOPES
from titan.gateway.security import (
    BodySizeCapMiddleware,
    DocsAuthGuardMiddleware,
    SecurityHeadersMiddleware,
)
from titan.gateway.slots import AuthenticationSlot, RBACSlot
from titan.handoffs import router as handoffs_router
from titan.handoffs.service import register_standard_gates
from titan.identity import router as identity_router
from titan.identity.tokens import KeyRing
from titan.llm import router as llm_router
from titan.llm import service as llm_service
from titan.logs import router as logs_router
from titan.logs.loki import HttpLokiQuerier
from titan.loop import router as loop_router
from titan.memory import router as memory_router
from titan.memory import service as memory_service
from titan.messaging import router as messaging_router
from titan.observability import otel
from titan.observability.health import router as health_router
from titan.observability.logging import configure_logging
from titan.observability.metrics import REDMetricsMiddleware
from titan.observability.metrics import router as metrics_router
from titan.orchestrator import router as tasks_router
from titan.planning import router as planning_router
from titan.plugins import router as plugins_router
from titan.plugins import service as plugins_service
from titan.projects import router as projects_router
from titan.realtime import router as realtime_router
from titan.realtime import service as realtime_service
from titan.redaction import RedactionReporter
from titan.redaction import runtime as redaction_runtime
from titan.runs import router as runs_router
from titan.runtime import service as runtime_service
from titan.settings import Settings, get_settings
from titan.tools import router as tools_router
from titan.tools import service as tools_service
from titan.tools.sandbox.session import SandboxSecretResolver
from titan.usage import router as usage_router
from titan.vault import bootstrap as vault_bootstrap
from titan.vault import inventory as vault_inventory
from titan.vault import router as secrets_router
from titan.vault import service as vault_service

logger = structlog.get_logger("titan.main")


def _build_sandbox_secret_resolver(application: FastAPI) -> "SandboxSecretResolver":
    """A TITAN-SEC-053 resolver closure over app state: task → assignee agent →
    roster grants → the tools' ``required_secrets`` → vault values, and an
    injection audit. Fast-paths to an empty map when no registered tool declares
    a sandbox secret need (the current state), so it costs nothing until a
    sandbox-executing tool actually needs a credential."""
    from sqlalchemy import text

    from titan.agents.definition import roster_id_for_role
    from titan.tools.sandbox.injection import resolve_sandbox_secrets

    async def _resolver(project_id: "uuid.UUID", task_id: "uuid.UUID | None") -> dict[str, str]:
        registry = application.state.tool_registry
        if task_id is None or not any(s.required_secrets for s in registry.specs()):
            return {}
        async with application.state.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT a.role FROM tasks t JOIN agents a "
                        "ON a.id = t.assignee_agent_id WHERE t.id = :tid"
                    ),
                    {"tid": task_id},
                )
            ).first()
        roster_id = roster_id_for_role(row.role) if row is not None else None
        agent_def = application.state.agent_registry.get(roster_id) if roster_id else None
        grants = agent_def.tool_grants if agent_def is not None else ()
        secrets = await resolve_sandbox_secrets(grants, registry, application.state.vault_provider)
        if secrets:
            async with application.state.engine.begin() as conn:
                await vault_service.record_injection(
                    conn,
                    actor=f"sandbox:{task_id}",
                    names=list(secrets),
                    task_id=str(task_id),
                )
        return secrets

    return _resolver


def _egress_config(settings: Settings) -> "tools_service.EgressConfig | None":
    """Build the sandbox egress wiring from settings, or None to keep deny-all."""
    if not settings.sandbox_egress_network or not settings.sandbox_forward_proxy_url:
        return None
    authority = settings.sandbox_forward_proxy_url.split("//", 1)[-1].split("/", 1)[0]
    host, _, port = authority.partition(":")
    return tools_service.EgressConfig(
        network=settings.sandbox_egress_network,
        proxy_host=host,
        proxy_port=int(port) if port else 8080,
        secret=settings.egress_proxy_secret,
        pip_index_url=settings.sandbox_pip_index_url,
        npm_registry_url=settings.sandbox_npm_registry_url,
        git_hosts=tuple(settings.sandbox_egress_git_hosts),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the titan-core FastAPI application."""
    active_settings = settings if settings is not None else get_settings()
    configure_logging(active_settings.log_level, service="titan-api")
    otel.setup_tracing(active_settings, service_name="titan-api")
    production = active_settings.environment == "production"

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # Phase 26: load + validate the declarative agent roster FIRST — a
        # malformed or incomplete roster (TITAN-AGT-001/008) raises here, so the
        # process refuses to boot rather than starting with broken authority.
        application.state.agent_registry = agents_service.load_roster(active_settings)
        application.state.engine = clients.create_engine(active_settings)
        application.state.redis = clients.create_redis(active_settings)
        application.state.http = clients.create_http_client(active_settings)
        # Phase 73 (Vol 8 §10): the ONE shared audit client every subsystem records
        # through — the immutable, hash-chained audit_events ledger (SEC-090..092).
        application.state.audit = audit_service.build_audit_client(application.state.engine)
        # Phase 72 (Vol 9 §5.3, TITAN-OPS-012): self-hosted feature flags + ops
        # kill switches. A two-level cache (in-process + shared Redis snapshot,
        # 10 s TTL) read through by the gateway (llm.provider_override) and the
        # intake/admission path; a pub/sub subscriber busts the local cache so a
        # flip converges across replicas within ~1 s (the worker reads the SAME
        # store for agents.pause_all on every dispatch cycle).
        application.state.flags = flags_service.build_flag_store(
            application.state.engine,
            application.state.redis,
            ttl_seconds=active_settings.cache_flags_ttl_seconds,
        )
        application.state.flags_subscriber = flags_service.FlagBustSubscriber(
            application.state.redis, application.state.flags
        )
        await application.state.flags_subscriber.start()
        # Phase 70: the TITAN-BCK-022 / SEC-080 Redis token-bucket rate limiter.
        # Built here so the route→class table sees the fully-mounted app; the
        # audit hook writes security_events without the gateway importing
        # identity (module boundary). Disabled → the middleware passes through.
        application.state.rate_limiter = None
        application.state.route_class_table = {}
        application.state.rate_limit_routes = []
        if active_settings.rate_limit_enabled:

            async def _rate_limit_audit(
                *, source_ip: str, user_id: "uuid.UUID | None", detail: dict[str, Any]
            ) -> None:
                from titan.identity import audit as identity_audit

                await identity_audit.record(
                    application.state.engine,
                    identity_audit.SecurityEventType.RATE_LIMITED,
                    user_id=user_id,
                    source_ip=source_ip,
                    detail=detail,
                )
                # Phase 73: also record to the unified immutable audit ledger via
                # the ONE shared client (SEC-090 §10.2 rate-limit block). Best-
                # effort — an audit blip must not turn a throttle into a 500.
                with contextlib.suppress(Exception):
                    await application.state.audit.record(
                        actor=str(user_id) if user_id is not None else "anonymous",
                        action=audit_service.AuditAction.RATELIMIT_BLOCK,
                        resource=str(detail.get("route", "*")),
                        outcome=audit_service.Outcome.DENIED,
                        source_ip=source_ip,
                        detail=detail,
                    )

            limiter = RateLimiter.build(
                application.state.redis,
                read=active_settings.rate_limit_read,
                write=active_settings.rate_limit_write,
                transition=active_settings.rate_limit_transition,
                auth=active_settings.rate_limit_auth,
                project_create=active_settings.rate_limit_project_create,
                per_ip=active_settings.rate_limit_per_ip,
                per_user=active_settings.rate_limit_per_user,
                per_org=active_settings.rate_limit_per_org,
                trusted_proxies=active_settings.trusted_proxies,
                org=active_settings.org,
                audit=_rate_limit_audit,
            )
            await limiter.load()
            from titan.gateway.route_guard import api_routes

            application.state.rate_limiter = limiter
            application.state.route_class_table = build_route_class_table(application)
            application.state.rate_limit_routes = api_routes(application)

        # Phase 70: WS abuse audit hook (SEC-081). realtime cannot import
        # identity's audit module (module-independence contract), so the record
        # is wired here at the composition root and read off app.state.ws_audit.
        async def _ws_audit(*, event: str, user_id: "uuid.UUID", detail: dict[str, Any]) -> None:
            from titan.identity import audit as identity_audit

            await identity_audit.record(
                application.state.engine,
                identity_audit.SecurityEventType(event),
                user_id=user_id,
                detail=detail,
            )

        application.state.ws_audit = _ws_audit
        # Phase 68: the secrets vault (Vol 8 §6, ADR-SEC-03) — the age store (or
        # the env fallback) for get-by-reference + write-only management, the
        # field cipher for at-rest column encryption (SEC-063), and the bus
        # envelope signer + audit hook for signed streams (SEC-020). Built at the
        # composition root so titan.events stays a leaf (signer/hook injected).
        vault_store, vault_provider = vault_bootstrap.build_secret_backend(active_settings)
        application.state.vault_store = vault_store
        application.state.vault_provider = vault_provider
        application.state.field_cipher = vault_bootstrap.build_field_cipher(active_settings)
        application.state.bus_signer = vault_bootstrap.build_envelope_signer(active_settings)
        _engine_for_audit = application.state.engine

        async def _audit_invalid_envelope(rejection: "events_service.InvalidEnvelope") -> None:
            await vault_service.record_envelope_rejection(
                _engine_for_audit,
                actor=rejection.actor,
                topic=rejection.topic,
                reason=rejection.reason,
                event_id=rejection.event_id,
                payload_digest=rejection.payload_sha256,
            )

        application.state.bus_invalid_hook = _audit_invalid_envelope
        # Phase 69: the SEC-054 redaction pipeline. The process-global engine
        # has been redacting (patterns + entropy) since configure_logging ran;
        # here it gains the live-inventory detector and the audit reporter.
        # The maintenance loop refreshes digests + flushes hit audits; vault
        # writes additionally refresh inline (see the secrets router).
        application.state.redaction_reporter = RedactionReporter()
        redaction_runtime.default_engine().set_reporter(application.state.redaction_reporter.record)
        application.state.redaction_stop = asyncio.Event()
        application.state.redaction_task = asyncio.create_task(
            vault_inventory.run_redaction_maintenance(
                application.state.engine,
                vault_provider,
                vault_store,
                application.state.redaction_reporter,
                service="titan-api",
                stop=application.state.redaction_stop,
            )
        )
        # SEC-082: the budget manager alerts Maintainers at the 80%/100% budget
        # crossings. llm may not import messaging/identity, so the notifier is
        # built here at the root and injected into the gateway.
        from titan.budget_alerts import build_budget_notifier

        application.state.llm_gateway = llm_service.build_gateway(
            active_settings,
            engine=application.state.engine,
            redis=application.state.redis,
            budget_notifier=build_budget_notifier(application.state.engine),
            # TITAN-OPS-012 kill switch: when llm.provider_override is ON, force
            # every call to the aliased provider. Injected as a callback so llm
            # never imports titan.flags (the composition-root pattern).
            provider_override=lambda: application.state.flags.value(
                flags_service.PROVIDER_OVERRIDE
            ),
        )
        # §6.1 reranker (Phase 36): cross-encoder (RERANKER=on, extra installed),
        # LLM listwise (llm / degraded on), or the zero-latency no-op (off) —
        # built once, reused across packet requests. (Its background warm-up is
        # created just before yield so a startup failure never orphans it.)
        application.state.memory_reranker = memory_service.build_reranker(
            active_settings, application.state.llm_gateway
        )
        application.state.reranker_warmup = None
        # §12/§11.1 reflection + miner LLM calls route to the cheapest configured
        # tier (TITAN-BRN-017); an unreadable models.yaml → None → gateway default.
        try:
            application.state.summary_model_alias = memory_service.cheapest_chat_alias(
                active_settings.models_config_path
            )
        except Exception:
            application.state.summary_model_alias = None
        # §4.1 boot-time registry rebuild + the single side-effect chokepoint.
        application.state.tool_registry = tools_service.build_registry()
        # Per-task workspace jail (fs.* tools) + the hardened sandbox controller
        # for code execution (Vol 8 §5); the latter degrades gracefully when no
        # Docker daemon is reachable.
        application.state.workspaces = tools_service.build_workspace_manager(
            active_settings.sandbox_workspace_root
        )
        # Phase 23: when an egress network is configured, the sandbox reaches the
        # internet ONLY through the audited forward proxy (deny-all otherwise).
        egress_config = _egress_config(active_settings)
        application.state.sandbox_controller = tools_service.build_sandbox_controller(
            active_settings.sandbox_image,
            docker_host=active_settings.sandbox_docker_host,
            egress=egress_config,
        )
        application.state.sandbox_manager = tools_service.build_sandbox_manager(
            application.state.sandbox_controller,
            secret_resolver=_build_sandbox_secret_resolver(application),
        )
        # Per-task rootless DinD sidecars (docker.* tools) + headless-browser
        # containers (browser.* tools); both degrade to TOOL_UNAVAILABLE when no
        # Docker daemon is reachable (e.g. the socket-less API container).
        application.state.dind_controller = tools_service.build_dind_controller(
            active_settings.dind_image,
            active_settings.dind_preloaded_images,
            docker_host=active_settings.sandbox_docker_host,
        )
        application.state.dind_manager = tools_service.build_dind_manager(
            application.state.dind_controller
        )
        application.state.browser_controller = tools_service.build_browser_controller(
            active_settings.browser_image, docker_host=active_settings.sandbox_docker_host
        )
        application.state.browser_manager = tools_service.build_browser_manager(
            application.state.browser_controller
        )
        # web.* research tools: a client to the caching fetch-gateway (Phase 23).
        # Absent a configured gateway URL, web.* fail closed (TOOL_UNAVAILABLE).
        application.state.research_client = None
        if active_settings.research_gateway_url:
            application.state.research_client = tools_service.build_research_client(
                clients.create_bare_http_client(),
                gateway_url=active_settings.research_gateway_url,
                token=active_settings.research_gateway_token,
            )
        application.state.tool_bus = tools_service.build_bus(
            application.state.engine,
            registry=application.state.tool_registry,
            workspace_provider=application.state.workspaces,
            sandbox_manager=application.state.sandbox_manager,
            dind_manager=application.state.dind_manager,
            browser_manager=application.state.browser_manager,
            research_client=application.state.research_client,
            redis=application.state.redis,
        )
        # Phase 52: the manifest-driven plugin runtime (Vol 7 §3). Plugin tools
        # contribute to the SAME registry the Tool Bus dispatches, so the supervisor is
        # built right after the bus. It spawns nothing here — workers start only when an
        # operator enables a plugin (or on resume of one left ENABLED).
        application.state.plugins_dir = active_settings.plugins_dir
        application.state.plugin_scopes = KNOWN_SCOPES
        application.state.plugin_cipher = plugins_service.ConfigCipher(
            active_settings.plugin_config_key
        )
        # Phase 68: one secret source. Plugins resolve their named secrets from
        # the SAME provider the vault exposes — the env fallback in the default
        # config (identical to Phase 52), or the age vault when one is unlocked
        # (SEC-050: integration secrets live in the vault, resolved by name).
        application.state.plugin_secrets = application.state.vault_provider
        application.state.plugin_supervisor = None
        application.state.plugin_health_stop = None
        application.state.plugin_health_task = None
        if active_settings.plugins_enabled:
            application.state.plugin_supervisor = plugins_service.PluginSupervisor(
                application.state.engine,
                application.state.tool_registry,
                secrets=application.state.plugin_secrets,
                http_fetcher=plugins_service.build_http_fetcher(application.state.http),
                # Phase 53: MCP Streamable-HTTP servers POST over the shared client, and
                # per-tool risk overrides are read from the encrypted config at enable time.
                http_client=application.state.http,
                config_cipher=application.state.plugin_cipher,
                call_timeout_s=active_settings.plugin_call_timeout_seconds,
                workspace_root=Path(active_settings.sandbox_workspace_root),
            )
            # Restore workers for plugins the operator left ENABLED; one that cannot
            # come back is quarantined rather than silently missing its tools.
            await plugins_service.resume_enabled(
                application.state.engine,
                application.state.plugin_supervisor,
                known_scopes=KNOWN_SCOPES,
            )
            # Periodic healthcheck monitor: the "or a failed healthcheck" half of the
            # auto-quarantine rule (TITAN-INT-014) — enable only checks health once.
            application.state.plugin_health_stop = asyncio.Event()
            application.state.plugin_health_task = asyncio.create_task(
                plugins_service.run_health_monitor(
                    application.state.plugin_supervisor,
                    stop=application.state.plugin_health_stop,
                )
            )
        # Loki backs the Logs Viewer's history (Vol 4 §6.7, Phase 62). It sits
        # behind the Compose `observability` profile, so `loki_url` is empty on
        # a default stack and the endpoint answers 503 naming the setting rather
        # than pretending the logs are empty. `None` IS the off switch the
        # router tests for — never default this to a client pointed at nothing,
        # or "no Loki" turns into a connection error on every request.
        application.state.loki = (
            HttpLokiQuerier(active_settings.loki_url, application.state.http)
            if active_settings.loki_url
            else None
        )
        # Realtime WS backend (TITAN-BCK-007): this replica's local connection
        # registry + a pub/sub subscriber forwarding fanned-out frames to its
        # sockets. The domain-stream → pub/sub fan-out consumer runs ONLY in the
        # worker (a single consumer preserves per-project seq order); every api
        # replica just subscribes and delivers to its local sockets.
        application.state.ws_org = active_settings.org
        application.state.ws_manager = realtime_service.build_manager()
        application.state.ws_subscriber = realtime_service.build_subscriber(
            application.state.redis, application.state.ws_manager
        )
        await application.state.ws_subscriber.start()
        # Phase-25 CEO intake: a direct WS publisher (streaming deltas) + config +
        # a set tracking in-flight intake background tasks so they are cancelled at
        # shutdown rather than logged as orphaned.
        application.state.ws_publisher = realtime_service.build_publisher(application.state.redis)
        application.state.ceo_intake_enabled = active_settings.ceo_intake_enabled
        application.state.ceo_intake_model_alias = active_settings.ceo_intake_model_alias
        application.state.intake_tasks = set()
        application.state.readyz_budget_seconds = active_settings.readyz_budget_seconds
        # Phase 32: the non-LLM Orchestrator watchdog (TITAN-AGT-009) — restarts
        # stalled inbox consumers, escalates unacked requires_ack messages (deduped
        # per dispute, §10 R6), and enforces discussion deadlines (TITAN-AGT-014).
        # Its thresholds live in watchdog_config and apply without redeploy.
        application.state.watchdog = None
        application.state.watchdog_stop = None
        application.state.watchdog_task = None
        if active_settings.watchdog_enabled:
            agent_runtime = runtime_service.build_runtime(
                active_settings,
                application.state.engine,
                redis=application.state.redis,
                llm=application.state.llm_gateway,
                tool_bus=application.state.tool_bus,
                flags=application.state.flags,
                # Phase 38 (§13): the real packet provider + working-memory
                # coordinator ride on the same Qdrant client and reranker the
                # packet endpoint uses — one retrieval stack, two entrances.
                http=application.state.http,
                reranker=application.state.memory_reranker,
            )
            watchdog = runtime_service.build_watchdog(
                agent_runtime,
                events_service.build_bus(
                    application.state.redis,
                    signer=application.state.bus_signer,
                    on_invalid=application.state.bus_invalid_hook,
                    maxlen=active_settings.stream_maxlen,
                ),
            )
            watchdog.register()
            application.state.watchdog = watchdog
            application.state.watchdog_stop = asyncio.Event()
            application.state.watchdog_task = asyncio.create_task(
                watchdog.run_forever(stop=application.state.watchdog_stop)
            )
        otel.instrument_engine(active_settings, application.state.engine)
        # Warm the cross-encoder model off the request path (TITAN-BRN-010: no
        # user request pays the load). Created LAST so a startup failure above
        # never orphans it. NOTE: cancel() cannot stop an in-flight load thread
        # (asyncio joins the executor at exit) — Phase 37's baked int8 artifact
        # makes the load fast; without it a mid-load shutdown may linger.
        if isinstance(application.state.memory_reranker, memory_service.CrossEncoderReranker):

            async def _warm_reranker() -> None:  # pragma: no cover — model path
                try:
                    await application.state.memory_reranker.warm_up()
                except Exception:
                    logger.exception("memory.reranker_warmup_failed")

            application.state.reranker_warmup = asyncio.create_task(_warm_reranker())
        try:
            yield
        finally:
            if application.state.watchdog is not None:  # stop the watchdog first
                application.state.watchdog_stop.set()
                application.state.watchdog_task.cancel()
                # Suppress ANY exception here: a service task that somehow died
                # earlier re-raises on await and must not abort the rest of the
                # shutdown chain (sockets, sandboxes, engine).
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await application.state.watchdog_task
                await application.state.watchdog.stop()
            for task in list(application.state.intake_tasks):  # cancel in-flight CEO turns
                task.cancel()
            # Stop the redaction maintenance loop via its event (NOT cancel):
            # the loop's exit path runs one final audit flush.
            application.state.redaction_stop.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(application.state.redaction_task, timeout=10.0)
            redaction_runtime.default_engine().set_reporter(None)
            await application.state.ws_subscriber.stop()  # stop pub/sub subscriber first
            if getattr(application.state, "flags_subscriber", None) is not None:
                await application.state.flags_subscriber.stop()  # flag-bust subscriber
            if application.state.plugin_health_task is not None:
                application.state.plugin_health_stop.set()
                application.state.plugin_health_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await application.state.plugin_health_task
            if application.state.plugin_supervisor is not None:
                # Terminate every plugin worker; in-flight calls fail structurally
                # rather than leaving orphaned subprocesses behind.
                await application.state.plugin_supervisor.aclose()
            await application.state.sandbox_manager.aclose()  # destroy live sandboxes
            await application.state.dind_manager.aclose()  # destroy DinD sidecars
            await application.state.browser_manager.aclose()  # destroy browser pages
            # Sweep any sidecar/browser containers orphaned by a crashed create.
            await application.state.dind_controller.destroy_all()
            await application.state.browser_controller.destroy_all()
            if application.state.research_client is not None:
                await application.state.research_client.aclose()
            if application.state.reranker_warmup is not None:
                application.state.reranker_warmup.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await application.state.reranker_warmup
            await application.state.llm_gateway.aclose()
            await application.state.http.aclose()
            await application.state.redis.aclose()
            await application.state.engine.dispose()

    app = FastAPI(
        title=active_settings.app_name,
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,  # one documented docs surface, guarded in production
        openapi_url="/api/openapi.json",
        debug=active_settings.debug,
        lifespan=lifespan,
    )

    # Gateway slot strategies. Phase 8 fills auth below. RBAC (Phase 9) is
    # enforced NOT by this middleware slot but by the per-route Requires
    # dependency (spec §3.4 "one enforcement point: a FastAPI dependency") —
    # permissions are route-specific, so a blanket middleware can't decide
    # them. The RBACSlot stays a documented pass-through: the §9.1 pipeline
    # position is reserved, the route-guard check guarantees every route
    # carries a Requires. Building a second middleware net here would be the
    # "second authorization mechanism" the phase forbids. Rate limiting
    # (TITAN-BCK-022, Phase 70) IS a real middleware — RateLimitMiddleware — that
    # reads app.state.rate_limiter (built in the lifespan) and passes through
    # when it is None.
    app.state.auth_strategy = None
    app.state.rbac_strategy = None
    app.state.keyring = KeyRing(active_settings)
    # Phase 67: the login path resolves the client IP from `trusted_proxies`,
    # and the throttle/MFA services read auth settings off request.app.state.
    app.state.settings = active_settings

    # §9.1 middleware order, outermost first:
    #   RED metrics (observability, transparent)
    #   → security headers (TITAN-BCK-019 — near-outermost so EVERY response,
    #     including short-circuits like 413/401/replays, carries them)
    #   → request-ID injection → CORS → authentication → RBAC → rate limiting
    #   → idempotency replay → docs guard → body-size cap → compression → router.
    # Starlette runs the LAST-added middleware OUTERMOST, so registration below
    # is that list reversed.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(BodySizeCapMiddleware)
    app.add_middleware(DocsAuthGuardMiddleware, production=production)
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RBACSlot)
    app.add_middleware(AuthenticationSlot)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, production=production)
    app.add_middleware(REDMetricsMiddleware)

    register_problem_handlers(app, production=production)
    app.include_router(health_router)
    app.include_router(metrics_router)
    identity_router.mount(app)  # /auth/* + fills the authentication slot
    projects_router.mount(app)  # /projects, /milestones, /sprints, board/graph
    tasks_router.mount(app)  # /tasks, /projects/{id}/tasks, dependencies, pipeline
    handoffs_router.mount(app)  # /projects/{id}/handoffs/* — artifacts + gate ledger
    agents_router.mount(app)  # /agents registry + :pause/:resume
    messaging_router.mount(app)  # /messages, /decisions, /notifications
    runs_router.mount(app)  # /runs, /tasks/{id}/runs, artifacts
    llm_router.mount(app)  # /llm/chat, /llm/embeddings — the provider gateway
    tools_router.mount(app)  # /projects/{id}/tool-approvals — admin-risk gate
    realtime_router.mount(app)  # /ws — JWT-authenticated realtime channel (ADR-009)
    memory_router.mount(app)  # /memory/events, /memory/records/{id} — the Memory Service
    constitution_router.mount(app)  # /constitution — versioned §6 standards artifact
    clarification_router.mount(app)  # /projects/{id}/clarifications — CEO §2.4 engine
    loop_router.mount(app)  # /tasks/{id}/loop — self-improvement loop audit trail
    deploy_router.mount(app)  # /projects/{id}/deployments — local deploy records (§6.7)
    debt_router.mount(app)  # /projects/{id}/debt — technical-debt ledger (WFL-021)
    logs_router.mount(app)  # /logs — Loki-backed log history for the viewer (§6.7)
    usage_router.mount(app)  # /usage — LLM cost/token aggregation for Monitoring (§6.10)
    planning_router.mount(app)  # /projects/{id}/sprint-plan, ceremonies, milestone, blockers (§4)
    artifacts_router.mount(app)  # /projects/{id}/artifacts — the six derived §5 artifacts
    plugins_router.mount(app)  # /plugins lifecycle + /hooks (HMAC) + /plugin-ui (Vol 7 §3)
    secrets_router.mount(app)  # /secrets — write-only vault management (Vol 8 §6)
    flags_router.mount(app)  # /flags — feature flags + ops kill switches (Vol 9 §5.3)
    audit_router.mount(app)  # /audit — immutable hash-chained audit ledger (Vol 8 §10)
    otel.instrument_app(active_settings, app)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        app.openapi_schema = problemify_openapi(schema)
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


app = create_app()
# Register the concrete G2–G5 acceptance gates (Vol 6 §2.3) at the composition
# root so real pipelines enforce them. Idempotent; tests drive the gate registry
# explicitly (the §2.3 predicates live in titan.handoffs.standard_gates).
register_standard_gates()
