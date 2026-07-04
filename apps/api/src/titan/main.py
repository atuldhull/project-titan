"""FastAPI application factory for titan-core (TITAN-BCK-001).

``uvicorn titan.main:app`` starts the full API. The lifespan owns the async
backing-service clients (created lazily, closed on shutdown); routers and
middleware accumulate here phase by phase on this single factory.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from titan import clients
from titan.gateway.middleware import RequestContextMiddleware
from titan.observability.health import router as health_router
from titan.observability.logging import configure_logging
from titan.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the titan-core FastAPI application."""
    active_settings = settings if settings is not None else get_settings()
    configure_logging(active_settings.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.engine = clients.create_engine(active_settings)
        application.state.redis = clients.create_redis(active_settings)
        application.state.http = clients.create_http_client(active_settings)
        application.state.readyz_budget_seconds = active_settings.readyz_budget_seconds
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
        openapi_url="/api/openapi.json",
        debug=active_settings.debug,
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    return app


app = create_app()
