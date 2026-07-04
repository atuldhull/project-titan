"""Phase-1 skeleton tests: entrypoints import cleanly and the factory works."""

from fastapi import FastAPI

from titan.main import app, create_app
from titan.settings import Settings


def test_create_app_returns_fastapi() -> None:
    built = create_app()
    assert isinstance(built, FastAPI)
    assert built.title == "TITAN"


def test_module_level_app_exists() -> None:
    assert isinstance(app, FastAPI)


def test_settings_defaults_are_local() -> None:
    settings = Settings()
    assert settings.environment == "local"
    assert settings.debug is False


def test_worker_entrypoint_imports() -> None:
    from titan import worker

    assert callable(worker.main)
