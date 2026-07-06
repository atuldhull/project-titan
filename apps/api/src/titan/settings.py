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

    # --- Worker / run lifecycle (Phase 14; TITAN-BCK-017, §8.2) ---
    # Max runs executed concurrently in ONE worker (asyncio task-group bound).
    worker_concurrency: int = 4
    # Soft per-run timeout; a run exceeding it becomes `timed_out` and retries.
    run_soft_timeout_seconds: float = 900.0
    # Filesystem root for the MVP artifact Storage backend (Vol 3 §6).
    artifact_root: str = "/var/lib/titan/artifacts"

    # --- Sandbox + workspaces (Phase 20; Vol 7 §4.3, Vol 8 §5, ADR-SEC-02) ---
    # Host root under which per-task workspace directories are created. Each
    # task gets its own subdir (bind-mounted rw at /workspace in its sandbox).
    sandbox_workspace_root: str = "/var/lib/titan/workspaces"
    # Base image for per-task sandboxes (hardened runc locally). The tooled
    # image (infra/docker/titan-sandbox.Dockerfile) carries git/python/node for
    # the core git/terminal/code tools; build it with
    # `docker build -f infra/docker/titan-sandbox.Dockerfile -t titan-sandbox:local .`
    sandbox_image: str = "titan-sandbox:local"
    # Empty = the docker SDK's default endpoint (unix socket / Windows npipe);
    # set to bind a specific daemon (e.g. tcp://... in CI).
    sandbox_docker_host: str = ""

    # --- docker.* tools: rootless DinD sidecar (Phase 22; Vol 7 §4.3, SEC-044) ---
    # Per-task rootless Docker-in-Docker daemon the docker.* tools talk to; the
    # HOST daemon socket is never mounted or reachable from it (TITAN-SEC-044).
    dind_image: str = "docker:dind-rootless"
    # Images seeded offline (host `save` | dind `load`) into each per-task dind
    # daemon so docker.build/compose can build FROM them with zero registry
    # egress (offline-first). Comma-separated or JSON list in TITAN_DIND_PRELOADED_IMAGES.
    dind_preloaded_images: Annotated[list[str], NoDecode] = ["titan-sandbox:local"]

    # --- browser.* tools: headless Chromium via Playwright (Phase 22; INT-032) ---
    # The container running `playwright run-server`; the trusted Tool Host
    # connects to its websocket. Build:
    # `docker build -f infra/docker/titan-browser.Dockerfile -t titan-browser:local .`
    browser_image: str = "titan-browser:local"

    # --- Egress + web tools (Phase 23; Vol 7 §5, Vol 8 §5 SEC-042/043) ---
    # The audited egress path. The forward proxy is the SOLE hop out of the
    # sandbox's deny-all internal network; the fetch-gateway is the caching
    # research proxy + pypi/npm mirror. Both run from the titan image as
    # `python -m titan.tools.egress`.
    # Which listener(s) `python -m titan.tools.egress` runs. In prod the forward
    # proxy (sandbox-facing) and the gateway (tool-host + mirror) run as SEPARATE
    # containers on different networks so the sandbox can reach ONLY the forward
    # proxy; "both" (default) is the single-process local convenience.
    egress_role: Literal["forward", "gateway", "both"] = "both"
    # Shared HMAC secret used to mint/verify the per-task proxy credential
    # (empty = an ephemeral dev secret; set in prod so tokens survive restarts).
    egress_proxy_secret: str = ""
    # Where the forward proxy listens (host:port inside its container).
    egress_forward_host: str = "0.0.0.0"
    egress_forward_port: int = 8080
    # Where the fetch-gateway listens.
    egress_gateway_host: str = "0.0.0.0"
    egress_gateway_port: int = 8090
    # The gateway URL the trusted Tool Host's web.* tools call (research fetch +
    # search). Empty = web tools are TOOL_UNAVAILABLE (no gateway wired).
    research_gateway_url: str = ""
    # Internal bearer the tool host presents to the gateway (defense in depth).
    research_gateway_token: str = ""
    # The internal docker network the sandbox joins for proxied egress; empty
    # keeps the Phase-20 deny-all `network_mode=none` (no egress at all).
    sandbox_egress_network: str = ""
    # The proxy/mirror URLs injected into the sandbox when egress is enabled.
    sandbox_forward_proxy_url: str = ""  # e.g. http://titan-egress:8080
    sandbox_pip_index_url: str = ""  # e.g. http://titan-egress-gateway:8090/pypi/simple/
    sandbox_npm_registry_url: str = ""  # e.g. http://titan-egress-gateway:8090/npm/
    # Git-remote hosts the sandbox may reach (SEC-043); comma-separated or JSON.
    sandbox_egress_git_hosts: Annotated[list[str], NoDecode] = []
    # Search backend for web.search: "searxng" (default) or "none".
    web_search_backend: str = "searxng"
    searxng_url: str = "http://searxng:8080"
    # Upstreams the gateway pull-through mirror + search proxy reach (override to
    # a local fixture in hermetic tests).
    egress_pypi_upstream: str = "https://pypi.org"
    egress_npm_upstream: str = "https://registry.npmjs.org"

    # --- LLM gateway (Phase 15; Vol 7 §8.2, ADR-007) ---
    # Path to config/models.yaml (hot-reloadable). The container mounts the repo
    # config dir; ship the local-only profile to run with zero paid API keys.
    models_config_path: str = "config/models.yaml"
    # Versioned price table for cost metering + budgets (Phase 17; §8.4).
    prices_config_path: str = "config/prices.yaml"

    # OTLP trace export (TITAN-BCK-025). Empty = tracing disabled (no
    # collector running); Compose obs profile sets http://otel-collector:4317.
    otel_exporter_otlp_endpoint: str = ""

    # --- Identity (Phase 8; TITAN-BCK-020, TITAN-SEC-011) ---
    # AUTH_MODE=local runs fully offline; oidc joins in M7 hardening.
    auth_mode: Literal["local", "oidc"] = "local"
    org: str = "titan"
    jwt_audience: str = "titan-api"
    jwt_issuer: str = "titan"
    # Base64-encoded RSA private key PEM; empty = ephemeral dev keypair.
    jwt_private_key_b64: str = ""

    # CORS allowlist (§9.1; dev default per spec). Env: comma-separated or
    # JSON list in TITAN_CORS_ORIGINS. NoDecode: the validator below owns
    # parsing (pydantic-settings would otherwise demand JSON and crash on
    # the comma-separated form).
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    @field_validator(
        "cors_origins", "dind_preloaded_images", "sandbox_egress_git_hosts", mode="before"
    )
    @classmethod
    def _parse_str_list(cls, value: object) -> object:
        if isinstance(value, str):
            if value.lstrip().startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
