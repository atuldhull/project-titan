"""FastAPI application factory for titan-core (TITAN-BCK-001).

``uvicorn titan.main:app`` starts the full API. The §9.1 gateway middleware
stack lives here, in its mandated order; routers and strategies accumulate
phase by phase on this single factory.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.utils import get_openapi

from titan import clients
from titan.gateway.idempotency import IdempotencyMiddleware
from titan.gateway.middleware import RequestContextMiddleware
from titan.gateway.openapi import problemify_openapi
from titan.gateway.problems import register_problem_handlers
from titan.gateway.security import (
    BodySizeCapMiddleware,
    DocsAuthGuardMiddleware,
    SecurityHeadersMiddleware,
)
from titan.gateway.slots import AuthenticationSlot, RateLimitSlot, RBACSlot
from titan.identity import router as identity_router
from titan.identity.tokens import KeyRing
from titan.observability import otel
from titan.observability.health import router as health_router
from titan.observability.logging import configure_logging
from titan.observability.metrics import REDMetricsMiddleware
from titan.observability.metrics import router as metrics_router
from titan.orchestrator import router as tasks_router
from titan.projects import router as projects_router
from titan.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the titan-core FastAPI application."""
    active_settings = settings if settings is not None else get_settings()
    configure_logging(active_settings.log_level, service="titan-api")
    otel.setup_tracing(active_settings, service_name="titan-api")
    production = active_settings.environment == "production"

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.engine = clients.create_engine(active_settings)
        application.state.redis = clients.create_redis(active_settings)
        application.state.http = clients.create_http_client(active_settings)
        application.state.readyz_budget_seconds = active_settings.readyz_budget_seconds
        otel.instrument_engine(active_settings, application.state.engine)
        try:
            yield
        finally:
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
    tasks_router.mount(app)  # /tasks, /projects/{id}/tasks, dependencies
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
