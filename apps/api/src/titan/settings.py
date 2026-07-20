"""Application settings — env-driven via pydantic-settings (Vol 3 §13.2).

All service configuration flows through environment variables (TITAN-BCK-026);
the checked-in ``.env.example`` at the repo root documents them.
"""

import ipaddress
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

    # §6.1 reranker mode (TITAN_RERANKER). "on" = in-process bge-reranker-v2-m3
    # cross-encoder (needs the optional `rerank` extra). "llm" = listwise rerank
    # through the gateway's cheapest chat model (§6.1's stated fallback). "off"
    # = a zero-latency rank-preserving no-op — fusion order + freshness/
    # importance decide (§15 R4 low-RAM mode; the only mode that can meet the
    # TITAN-BRN-010 ≤400 ms reranker-off budget, which an LLM round-trip cannot;
    # the §6.1-vs-§6.4 tension is resolved by offering both as distinct modes).
    reranker: Literal["on", "llm", "off"] = "on"
    # The cross-encoder for RERANKER=on: a HF model id or a local directory
    # (scripts/fetch_reranker.py packages one with int8 ONNX weights). The
    # default is the model that MEETS TITAN-BRN-010 on the reference laptop
    # profile: §6.1 names bge-reranker-v2-m3 at "~30 ms per pair batch", but
    # its measured int8 cost is ~180 ms/pair on the reference CPU (Ryzen
    # 8845HS, AVX-512 VNNI) — 4.3 s per 24-pair batch, 5x the entire 800 ms
    # budget. The numbered §6.4 MUST outranks the §6.1 prose (the Phase-36
    # precedent), so the laptop default is a small cross-encoder inside the
    # budget, and bge-reranker-v2-m3 stays one config switch away for beefier
    # hosts / multilingual orgs (the §15 R1 "config-only upgrade" philosophy).
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    # Explicit ONNX weight file for the cross-encoder, relative to the model
    # directory (e.g. the int8 export scripts/fetch_reranker.py produces and
    # the image bakes, §6.1/§6.3). None = the library default (which may
    # trigger a slow in-process export for models without published ONNX
    # weights).
    reranker_onnx_file: str | None = None
    # Token cap per (query, chunk) rerank pair. Cross-encoder cost scales
    # linearly with sequence length: 24 full 512-token chunks cost ~970 ms
    # even on the small default model — over the TITAN-BRN-010 budget — vs
    # ~365 ms truncated to 256 (measured, reference laptop). Relevance signal
    # is front-loaded (each chunk leads with its contextual header + opening),
    # so truncation is the standard latency/quality trade.
    reranker_max_length: int = 256

    # /readyz dependency-check budget in seconds (Vol 3 §12: 500 ms)
    readyz_budget_seconds: float = 0.5

    # --- Worker / run lifecycle (Phase 14; TITAN-BCK-017, §8.2) ---
    # Max runs executed concurrently in ONE worker (asyncio task-group bound).
    worker_concurrency: int = 4
    # Soft per-run timeout; a run exceeding it becomes `timed_out` and retries.
    run_soft_timeout_seconds: float = 900.0
    # Phase 56: run scheduled task runs through the REAL agent turn (LiveExecutor)
    # instead of the StubExecutor placeholder. Off by default so the delivery
    # worker stays deterministic in CI/cloud; the offline acceptance run sets it.
    live_executor: bool = False
    # Filesystem root for the MVP artifact Storage backend (Vol 3 §6).
    artifact_root: str = "/var/lib/titan/artifacts"
    # Idle poll interval for the outbox drainer. Small so committed events reach
    # the queue (and thus the WS fan-out) fast enough for the §5 250 ms budget;
    # a busy drainer never sleeps (it only pauses when the outbox is empty).
    outbox_drain_idle_seconds: float = 0.1

    # --- Delivery-pipeline gates & clarification (Phase 43; Vol 6 §2.3–2.4) ---
    # Per-sprint capacity in ideal agent-days used by gate G4 (H4) to reject an
    # over-capacity sprint. A deploy-time default; the Architect can plan below it.
    sprint_capacity_days: float = 5.0
    # Non-blocking CEO clarification window (TITAN-WFL-008): if the user does not
    # answer within this many hours, the pipeline proceeds with best-judgment
    # assumptions flagged for the release approval. Configurable per project.
    clarification_timeout_hours: int = 24
    # Hard cap on self-improvement loop cycles per task (TITAN-WFL-013); the engine
    # clamps to the configurable 2–5 range. On exhaustion the task escalates.
    loop_max_cycles: int = 3

    # --- Autonomous planning (Phase 50; Vol 6 §4, TITAN-WFL-015/019) ---
    # Parallel-agent capacity the sprint planner packs each sprint to (WFL-015).
    planning_parallel_capacity: int = 4
    # Blocker sweep cadence (WFL-019 requires recovery within one ≤60s tick).
    blocker_sweep_interval_seconds: float = 30.0
    # A task with no state change for longer than this multiple of its estimate is
    # flagged BLOCKED/stalled (WFL-019: "beyond 2× its estimate").
    blocker_stall_multiplier: float = 2.0
    # Converts a story-point estimate to a wall-clock stall budget (ideal hours per
    # point) for the stall check.
    blocker_hours_per_point: float = 4.0
    # Estimate assumed for a task that carries no story-point estimate.
    blocker_default_points: int = 2

    # --- Derived project artifacts (Phase 51; Vol 6 §5, TITAN-WFL-020) ---
    # How often the reconciler regenerates the six derived artifacts for active
    # projects (idempotent, version-on-change) so they never go stale.
    artifact_reconcile_interval_seconds: float = 30.0

    # --- Plugin runtime (Phase 52; Vol 7 §3, TITAN-INT-010..017) ---
    # Off by default: enabling it lets the host spawn out-of-process plugin workers.
    plugins_enabled: bool = False
    # Where plugin directories (each with a titan-plugin.yaml) are discovered.
    plugins_dir: str = "config/plugins"
    # Default per-call ceiling for a plugin tool (§3.5: 60s, per-tool overridable).
    # A hanging plugin cannot block core — the call is bounded and returns TIMEOUT.
    plugin_call_timeout_seconds: float = 60.0
    # Key material for AES-256-GCM encryption of plugin config at rest (SEC-063).
    # Empty = an ephemeral dev key: config does not survive a restart, which is the
    # intended pressure to set one in production.
    plugin_config_key: str = ""

    # --- CEO intake handler (Phase 25 walking skeleton) ---
    # When a project brief is created, run the thin CEO intake turn (stream an LLM
    # response, publish run.output.delta over the WS, persist user stories). Opt-in
    # so ordinary project creation stays a pure CRUD write; the DoD run enables it.
    ceo_intake_enabled: bool = False
    # The model alias the CEO intake turn uses (a local Ollama alias by default so
    # the slice runs at $0; TITAN-FND-054).
    ceo_intake_model_alias: str = "local-default"

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

    # --- Agent roster (Phase 26; Vol 2 §2.1, TITAN-AGT-001/008) ---
    # Directory of declarative <agent_id>.yaml role contracts, loaded + validated
    # at startup — a malformed or incomplete roster refuses to boot. Relative
    # paths are resolved by walking up from the CWD (loader.resolve_config_dir);
    # the container mounts the repo config/ and sets an absolute path.
    agents_config_dir: str = "config/agents"

    # --- Orchestrator watchdog (Phase 32; Vol 2 §4.2, TITAN-AGT-009/020) ---
    # Run the non-LLM watchdog service in the API lifespan (stalled-consumer
    # restart, unacked requires_ack escalation deduped per dispute, discussion
    # deadline enforcement). Its thresholds live in the watchdog_config table and
    # take effect without redeploy. Off by default until the Phase-41 run loop
    # supervises live agent workers.
    watchdog_enabled: bool = False

    # --- Prompt composition + model policy (Phase 28; Vol 2 §8) ---
    # Jinja2 system-prompt templates root (templates are agents/<id>.j2 under it).
    agent_prompts_dir: str = "config/prompts"
    # agent -> tier -> provider/model policy (TITAN-AGT-021, config-only switching).
    model_policy_path: str = "config/model-policy.yaml"
    # Active model-policy profile: None = cloud tiers; "local-free" = offline Ollama.
    model_policy_profile: str | None = None

    # OTLP trace export (TITAN-BCK-025). Empty = tracing disabled (no
    # collector running); Compose obs profile sets http://otel-collector:4317.
    otel_exporter_otlp_endpoint: str = ""

    # Loki, backing the Logs Viewer's history endpoint (Vol 4 §6.7, Phase 62).
    # Empty = GET /api/v1/logs answers 503 naming this setting. Loki sits behind
    # the same Compose obs profile as the collector above, so empty is the
    # normal state of a default `docker compose up`, not a misconfiguration.
    loki_url: str = ""

    # --- Identity (Phase 8; TITAN-BCK-020, TITAN-SEC-011) ---
    # AUTH_MODE=local runs fully offline; oidc joins in M7 hardening.
    auth_mode: Literal["local", "oidc"] = "local"
    org: str = "titan"
    jwt_audience: str = "titan-api"
    jwt_issuer: str = "titan"
    # Base64-encoded RSA private key PEM; empty = ephemeral dev keypair.
    jwt_private_key_b64: str = ""

    # Proxies whose `X-Forwarded-For` we believe (Phase 67; TITAN-SEC-017).
    # CIDRs or bare addresses, comma-separated or a JSON list.
    #
    # EMPTY IS THE SAFE DEFAULT and means "trust no forwarding header" — the
    # socket peer is used instead. Correct for a direct-to-API deployment, and
    # deliberately wrong-but-safe for a proxied one: per-IP throttling degrades
    # to per-proxy (everyone shares one bucket) rather than silently honouring
    # a header any client can forge to bypass the throttle entirely, or to
    # forge someone else's address and get THEM throttled. Set this to the
    # BFF's address range wherever browsers reach the API through the proxy.
    trusted_proxies: Annotated[list[str], NoDecode] = []

    # CORS allowlist (§9.1; dev default per spec). Env: comma-separated or
    # JSON list in TITAN_CORS_ORIGINS. NoDecode: the validator below owns
    # parsing (pydantic-settings would otherwise demand JSON and crash on
    # the comma-separated form).
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    @field_validator(
        "cors_origins",
        "dind_preloaded_images",
        "sandbox_egress_git_hosts",
        "trusted_proxies",
        mode="before",
    )
    @classmethod
    def _parse_str_list(cls, value: object) -> object:
        if isinstance(value, str):
            if value.lstrip().startswith("["):
                return json.loads(value)
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def _validate_cidrs(cls, value: list[str]) -> list[str]:
        """Reject a malformed CIDR at startup rather than at request time.

        A typo here fails OPEN in the worst way: the resolver skips entries it
        cannot parse, so ``10.0.0.0/33`` would silently mean "this proxy is not
        trusted", every forwarded header would be ignored, and per-IP throttling
        would quietly collapse to per-proxy — the exact failure this setting
        exists to prevent, arriving with no error anywhere. Refusing to boot is
        the only reading that cannot be mistaken for working.
        """
        for entry in value:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as error:
                raise ValueError(
                    f"TITAN_TRUSTED_PROXIES entry {entry!r} is not a valid IP "
                    f"address or CIDR block: {error}"
                ) from error
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
