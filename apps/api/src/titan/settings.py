"""Application settings — env-driven via pydantic-settings (Vol 3 §13.2).

All service configuration flows through environment variables (TITAN-BCK-026);
the checked-in ``.env.example`` at the repo root documents them.
"""

import ipaddress
import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator, model_validator
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

    # --- Secrets vault (Phase 68; Vol 8 §6, ADR-SEC-03) ---
    # The age identity (AGE-SECRET-KEY-1…) that unlocks the age-encrypted YAML
    # vault at boot. EMPTY = no writable vault: secrets resolve read-only from
    # the environment (the Phase-52 default) and the write-only secrets API
    # answers 503. Generate one with `age-keygen` or `python -m titan.vault`.
    # This is a class-1 platform secret — never commit it; set it via env.
    vault_age_key: str = ""
    # Path to the age-encrypted secrets YAML (ciphertext; git-storable). Written
    # atomically by create/rotate/delete when a vault_age_key is set.
    vault_age_file: str = "config/secrets.yaml.age"
    # Key material for AES-256-GCM field encryption of the highest-sensitivity
    # columns (mfa_credentials.secret today; TITAN-SEC-063). Empty = an ephemeral
    # dev key in LOCAL only; staging/production refuse to boot without it (see
    # _require_crypto_keys_outside_local) because an ephemeral key locks every
    # MFA user out after a restart.
    field_encryption_key: str = ""
    # The PREVIOUS field key during a rotation window: new writes use
    # field_encryption_key, but rows written under the old key still decrypt
    # (TITAN-SEC-063 "decrypt old rows with old key version"). Clear it once a
    # re-encrypt pass has migrated every row.
    field_encryption_key_previous: str = ""
    # HMAC key for signed Redis Streams envelopes (TITAN-SEC-020). Empty derives
    # a fixed, well-known DEV key (so sign/verify is exercised locally without
    # wedging cross-process); set from the vault in production.
    bus_hmac_key: str = ""

    # --- Rate limiting (Phase 70; TITAN-BCK-022, TITAN-SEC-080) ---
    # A Redis token-bucket (atomic Lua/EVALSHA) layered per-IP (pre-auth),
    # per-user, and per-route-class. Disabling it makes the RateLimitMiddleware
    # a pass-through (used by tests that assert raw handler behaviour). The
    # limiter FAILS OPEN on a Redis outage (like the login throttle): a cache
    # blip must not become a total API outage. Limits are "count per window
    # seconds" with a burst ceiling (the bucket capacity); refill is
    # count/window tokens per second. All are env-configurable (TITAN-BCK-026);
    # "per-org" is per-deployment here (single-org: `org`).
    rate_limit_enabled: bool = True
    # Route classes (requests / window-seconds / burst). Read = GET; write =
    # POST/PATCH/PUT/DELETE; transition = :transition + run creation; auth = the
    # login/token surface (per-IP); project_create = project creation.
    rate_limit_read: str = "300/60/60"
    rate_limit_write: str = "60/60/20"
    rate_limit_transition: str = "30/60/30"
    rate_limit_auth: str = "10/60/10"
    rate_limit_project_create: str = "10/3600/10"
    # Layered coarse buckets that apply to every request regardless of class.
    rate_limit_per_ip: str = "60/60/60"  # pre-auth, per client IP
    rate_limit_per_user: str = "600/60/120"  # per authenticated principal
    rate_limit_per_org: str = "6000/60/600"  # per deployment/org

    # --- WebSocket abuse controls (Phase 70; TITAN-SEC-081) ---
    # Max concurrent connections per user (connect-time, cross-replica via
    # Redis) — Vol 3 §10 says 5, Vol 8 §9 says "<= 10"; 5 satisfies the <=10
    # ceiling and is the stricter reading, so it stands. Inbound frames are
    # capped at ws_max_messages_per_second per connection; sustained breach
    # (> burst tokens spent) triggers backpressure then disconnect + an audit
    # event.
    ws_max_connections_per_user: int = 5
    ws_max_messages_per_second: int = 20
    ws_message_burst: int = 40  # short bursts tolerated before disconnect

    # --- Read-through cache (Phase 70; TITAN-BCK-023) ---
    # Hot GETs (project, board, agents) served read-through from Redis, keyed by
    # a dedicated per-resource version counter bumped on write via the outbox
    # consumer (NOT the row version, which does not move on a task transition).
    # Fails open: a Redis miss/blip just re-reads Postgres.
    cache_enabled: bool = True
    cache_project_ttl_seconds: int = 300
    cache_agents_ttl_seconds: int = 60
    # Feature-flag snapshot TTL (Phase 72, Vol 9 §5.3). Short: a flag flip must
    # take effect within 15 s even if the pub/sub bust is missed, so the TTL is
    # the fallback propagation bound.
    cache_flags_ttl_seconds: int = 10

    # --- Inbound webhooks (Phase 70; TITAN-SEC-075) ---
    # Reject deliveries whose signed timestamp is older than this (skew). Applies
    # only to schemes that sign a timestamp; body-only HMAC (GitHub
    # X-Hub-Signature-256) has no timestamp and is protected by replay dedup.
    webhook_max_skew_seconds: int = 300
    # How long a processed delivery's replay marker is retained. DECOUPLED from
    # the skew window (Phase-70 review): body-only-HMAC schemes have no timestamp,
    # so this dedup horizon is their ONLY replay protection — it must be long
    # (default 24h), not the 5-minute skew window.
    webhook_replay_ttl_seconds: int = 86_400

    # --- Agent loop detection (Phase 71; TITAN-SEC-083) ---
    # A runaway agent is halted BEFORE the offending tool call: >= loop_max_repeats
    # near-identical CONSECUTIVE tool calls for a task, or more than
    # loop_step_budget total tool calls for a task, pauses it for review (a rung-3
    # escalation + audit event) within N+1 iterations. State is per task (Redis).
    loop_detection_enabled: bool = True
    loop_max_repeats: int = 3
    loop_step_budget: int = 120

    # --- Admission control & backpressure (Phase 72; TITAN-OPS-020) ---
    # Per-project concurrency quota: a project with this many non-terminal tasks
    # (the ledger's outstanding work) gets 429 + Retry-After on new intake, so one
    # project can never monopolise the execution tier. Accepted work is always
    # ledgered and waits in a queued state (never lost). 0 disables the quota.
    per_project_concurrent_task_quota: int = 25
    # Retry-After (seconds) advertised when the quota is hit — a client backs off
    # and retries; the durable ledger loses nothing.
    admission_retry_after_seconds: int = 30
    # Redis Streams XADD MAXLEN cap (~1M, TITAN-OPS-020). The backpressure chain
    # bounds queue growth here; the ledger, not the stream, is the durability
    # boundary (a lost queue is rebuilt by ops/scripts/requeue-from-ledger.py).
    stream_maxlen: int = 1_000_000

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

    @model_validator(mode="after")
    def _forbid_debug_outside_local(self) -> "Settings":
        """Refuse ``debug=True`` outside local (Phase 70 security review, SEC-076).

        FastAPI is built with ``debug=self.debug`` (main.py). When debug is on,
        Starlette's ``ServerErrorMiddleware`` renders an HTML traceback and
        BYPASSES the ``@app.exception_handler(Exception)`` generic-500 handler —
        so a stack trace would reach clients despite the RFC 9457 problem
        handler, defeating SEC-076 entirely. Like the crypto-key and
        trusted-proxy guards, a warning is not enough: refusing to boot is the
        only reading that cannot be mistaken for working.
        """
        if self.debug and self.environment != "local":
            raise ValueError(
                "debug=True is forbidden when environment is "
                f"{self.environment!r}: Starlette's debug traceback bypasses the "
                "generic-error handler and would leak stack traces to clients "
                "(TITAN-SEC-076). Set TITAN_DEBUG=false outside local."
            )
        return self

    @model_validator(mode="after")
    def _require_crypto_keys_outside_local(self) -> "Settings":
        """Fail CLOSED on empty crypto keys anywhere but local (Phase 68 security
        review). Both keys derive a usable-but-insecure fallback when empty —
        ``bus_hmac_key`` a source-visible fixed HMAC key (so a forgotten
        ``TITAN_BUS_HMAC_KEY`` would silently sign/verify SEC-020 envelopes with
        a public key, making forgery trivial — the exact fail-open the whole
        control exists to prevent), and ``field_encryption_key`` an ephemeral
        per-process AES key (so a forgotten ``TITAN_FIELD_ENCRYPTION_KEY`` would
        lock every MFA-enrolled user out after any restart or on any second
        worker). A warning is not enough — like ``trusted_proxies`` above and
        ``seed.py``'s refusal to seed a default admin password outside local,
        refusing to boot is the only reading that cannot be mistaken for
        working. ``vault_age_key`` is intentionally NOT required: an empty vault
        is a supported prod mode (env-backed secrets, SEC-050) and the write API
        answers 503 rather than failing open.
        """
        if self.environment == "local":
            return self
        missing = [
            name
            for name, value in (
                ("TITAN_BUS_HMAC_KEY", self.bus_hmac_key),
                ("TITAN_FIELD_ENCRYPTION_KEY", self.field_encryption_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"{', '.join(missing)} must be set when environment is "
                f"{self.environment!r} — an empty value would fall back to an "
                "insecure default (a public HMAC key / an ephemeral AES key). "
                "Set them from the vault, or run with environment=local for dev."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
