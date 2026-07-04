"""Application settings — env-driven via pydantic-settings (Vol 3 §13.2).

All service configuration flows through environment variables (TITAN-BCK-026);
the checked-in ``.env.example`` at the repo root documents them.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
