"""FastAPI application factory for titan-core (TITAN-BCK-001).

``uvicorn titan.main:app`` starts the full API. The middleware stack, routers,
and dependency wiring are added by later phases; the factory shape is fixed
here so every phase extends the same entrypoint.
"""

from fastapi import FastAPI

from titan.settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the titan-core FastAPI application."""
    active_settings = settings if settings is not None else get_settings()
    return FastAPI(
        title=active_settings.app_name,
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        debug=active_settings.debug,
    )


app = create_app()
