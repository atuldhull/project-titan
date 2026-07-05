"""Application settings — env-driven via pydantic-settings (Vol 3 §13.2).

All service configuration flows through environment variables (TITAN-BCK-026);
the checked-in ``.env.example`` at the repo root documents them.
"""

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for titan-core and titan-worker."""

    model_config = SettingsConfigDict(
        env_prefix="TITAN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "TITAN"
    environment: Literal["local", "staging", "production"] = "local"
    debug: bool = False
    log_level: str = "INFO"

    # Backing services (Vol 3 §13.1; container hostnames in Docker Compose)
    database_url: str = "postgresql+asyncpg://titan:titan@localhost:5432/titan"
    redis_url: str = "redis://localhost:6379/0"
    qdrant_url: str = "http://localhost:6333"

    # /readyz dependency-check budget in seconds (Vol 3 §12: 500 ms)
    readyz_budget_seconds: float = 0.5

    # OTLP trace export (TITAN-BCK-025). Empty = tracing disabled (no
    # collector running); Compose obs profile sets http://otel-collector:4317.
    otel_exporter_otlp_endpoint: str = ""

    # CORS allowlist (§9.1; dev default per spec). Env: comma-separated or
    # JSON list in TITAN_CORS_ORIGINS. NoDecode: the validator below owns
    # parsing (pydantic-settings would otherwise demand JSON and crash on
    # the comma-separated form).
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            if value.lstrip().startswith("["):
                return json.loads(value)
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
