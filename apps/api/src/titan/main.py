"""FastAPI application factory for titan-core (TITAN-BCK-001).

``uvicorn titan.main:app`` starts the full API. The §9.1 gateway middleware
stack lives here, in its mandated order; routers and strategies accumulate
phase by phase on this single factory.
"""

import asyncio
import contextlib
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
from titan.clarification import router as clarification_router
from titan.constitution import router as constitution_router
from titan.debt import router as debt_router
from titan.deploy import router as deploy_router
from titan.events import service as events_service
from titan.gateway.idempotency import IdempotencyMiddleware
from titan.gateway.middleware import RequestContextMiddleware
from titan.gateway.openapi import problemify_openapi
from titan.gateway.problems import register_problem_handlers
from titan.gateway.rbac import KNOWN_SCOPES
from titan.gateway.security import (
    BodySizeCapMiddleware,
    DocsAuthGuardMiddleware,
    SecurityHeadersMiddleware,
)
from titan.gateway.slots import AuthenticationSlot, RateLimitSlot, RBACSlot
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
from titan.runs import router as runs_router
from titan.runtime import service as runtime_service
from titan.settings import Settings, get_settings
from titan.tools import router as tools_router
from titan.tools import service as tools_service

logger = structlog.get_logger("titan.main")


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
        application.state.llm_gateway = llm_service.build_gateway(
            active_settings,
            engine=application.state.engine,
            redis=application.state.redis,
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
            application.state.sandbox_controller
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
        application.state.plugin_secrets = plugins_service.EnvSecretsProvider()
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
                # Phase 38 (§13): the real packet provider + working-memory
                # coordinator ride on the same Qdrant client and reranker the
                # packet endpoint uses — one retrieval stack, two entrances.
                http=application.state.http,
                reranker=application.state.memory_reranker,
            )
            watchdog = runtime_service.build_watchdog(
                agent_runtime, events_service.build_bus(application.state.redis)
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
            await application.state.ws_subscriber.stop()  # stop pub/sub subscriber first
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
    # "second authorization mechanism" the phase forbids. Rate-limit: Phase 13.
    app.state.auth_strategy = None
    app.state.rbac_strategy = None
    app.state.rate_limit_strategy = None
    app.state.keyring = KeyRing(active_settings)

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
    app.add_middleware(RateLimitSlot)
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
    planning_router.mount(app)  # /projects/{id}/sprint-plan, ceremonies, milestone, blockers (§4)
    artifacts_router.mount(app)  # /projects/{id}/artifacts — the six derived §5 artifacts
    plugins_router.mount(app)  # /plugins lifecycle + /hooks (HMAC) + /plugin-ui (Vol 7 §3)
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
