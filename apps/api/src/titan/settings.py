"""Application settings — env-driven via pydantic-settings (Vol 3 §13.2).

All service configuration flows through environment variables (TITAN-BCK-026);
a checked-in ``.env.example`` documents them from Phase 2 onward.
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


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
