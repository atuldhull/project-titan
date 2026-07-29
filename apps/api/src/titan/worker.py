"""Stream-consumer entrypoint for titan-worker (Vol 3 §2.1, TITAN-BCK-003).

Runs from the same image as titan-core with entrypoint ``python -m titan.worker``.
It runs the events backbone (outbox drainer, demo domain consumer, reaper) AND
the run lifecycle (Phase 14, §8.2): a bounded run-executor consuming
stream:runs.execute, its reaper (retry + DLQ), and a results consumer that drives
the state machine (success → submit_review).

Each loop is independently resilient (self-healing on transient errors) and
runs under ``gather(return_exceptions=True)`` so one dying loop cannot tear the
others down. A Prometheus metrics endpoint is exposed so DLQ/stream-depth
gauges are scrapeable (TITAN-BCK-011).
"""

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog
from prometheus_client import start_http_server
from sqlalchemy.ext.asyncio import AsyncEngine

from titan import clients
from titan.artifacts import service as artifacts
from titan.audit import service as audit_service
from titan.clarification import service as clarification
from titan.events import service as events
from titan.events.consumers import run_domain_consumer, run_drainer, run_reaper
from titan.events.outbox import OutboxDrainer
from titan.flags import service as flags_service
from titan.llm.service import build_gateway
from titan.memory import service as memory
from titan.observability.logging import configure_logging
from titan.observability.metrics import AUDIT_CHAIN_INTACT
from titan.orchestrator import service as orchestrator
from titan.planning import service as planning
from titan.realtime import service as realtime
from titan.redaction import RedactionReporter
from titan.redaction import runtime as redaction_runtime
from titan.runs.coordinator import (
    run_execute_reaper,
    run_executor_loop,
    run_results_loop,
    run_results_reaper,
)
from titan.runs.executor import StubExecutor
from titan.runs.live_executor import LiveExecutor, make_role_resolver
from titan.runs.storage import FilesystemStorage
from titan.runtime.service import build_runtime
from titan.settings import get_settings
from titan.vault import age as vault_age
from titan.vault import alerts as vault_alerts
from titan.vault import bootstrap as vault_bootstrap
from titan.vault import inventory as vault_inventory
from titan.vault import service as vault_service

logger = structlog.get_logger("titan.worker")

METRICS_PORT = 9100

# How often the orchestrator seeds pipelines for projects created while it was
# down. In-flight pipelines resume structurally (their cursor lives in Postgres),
# so this only needs to be brisk enough to satisfy the "replacement resumes within
# 30 seconds" DoD for projects with no pipeline yet.
PIPELINE_RECONCILE_INTERVAL_S = 15.0

# How often to resolve clarification batches past their non-blocking deadline
# (TITAN-WFL-008). Deadlines are hours out (default 24h), so a minute-scale tick is
# ample; the resolver is idempotent and per-project serialized.
CLARIFICATION_TIMEOUT_INTERVAL_S = 60.0

# How often the Project Manager sweeps for blocked tasks and runs the recovery
# ladder (TITAN-WFL-019 requires recovery within one ≤60s tick). Overridable via
# settings; defaulted below the 60s ceiling.
BLOCKER_SWEEP_INTERVAL_S = 30.0


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:  # pragma: no cover
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def run_pipeline_reconciler(  # pragma: no cover — live-verified loop
    engine: AsyncEngine, *, stop: asyncio.Event
) -> None:
    """Seed pipelines for un-seeded projects on a fixed cadence (TITAN-WFL-001).
    Self-healing: a transient DB error is logged and retried, never propagated."""
    while not stop.is_set():
        try:
            async with engine.begin() as conn:
                seeded = await orchestrator.reconcile_pipelines(conn)
            if seeded:
                logger.info("pipeline.reconciled", seeded=seeded)
        except Exception:
            logger.exception("pipeline.reconciler_error")
        await _sleep_or_stop(stop, PIPELINE_RECONCILE_INTERVAL_S)


async def run_clarification_timeout_reaper(  # pragma: no cover — live-verified loop
    engine: AsyncEngine, *, stop: asyncio.Event
) -> None:
    """Resolve clarification batches past their non-blocking deadline (TITAN-WFL-008):
    proceed with best-judgment assumptions flagged high-visibility for the release
    approval. Self-healing: a transient DB error is logged and retried."""
    while not stop.is_set():
        try:
            resolved = await clarification.resolve_timed_out_batches(engine)
            if resolved:
                logger.info("clarification.timed_out", resolved=resolved)
        except Exception:
            logger.exception("clarification.timeout_reaper_error")
        await _sleep_or_stop(stop, CLARIFICATION_TIMEOUT_INTERVAL_S)


async def run_blocker_sweeper(  # pragma: no cover — live-verified loop
    engine: AsyncEngine, *, stop: asyncio.Event
) -> None:
    """Detect blocked tasks system-wide and run the recovery ladder on a ≤60s tick
    (TITAN-WFL-019). Self-healing: a transient DB error is logged and retried."""
    interval = get_settings().blocker_sweep_interval_seconds
    while not stop.is_set():
        try:
            result = await planning.sweep_blockers(engine)
            if result.detected or result.advanced or result.recovered or result.escalated:
                logger.info(
                    "planning.blocker_sweep",
                    detected=result.detected,
                    advanced=result.advanced,
                    recovered=result.recovered,
                    escalated=result.escalated,
                )
        except Exception:
            logger.exception("planning.blocker_sweep_error")
        await _sleep_or_stop(stop, interval)


async def run_artifact_reconciler(  # pragma: no cover — live-verified loop
    engine: AsyncEngine, *, stop: asyncio.Event
) -> None:
    """Regenerate the six derived §5 artifacts for every active project on a cadence
    (TITAN-WFL-020 "never stale"). Idempotent — a regeneration versions only on a
    content change. Self-healing: a transient DB error is logged and retried."""
    interval = get_settings().artifact_reconcile_interval_seconds
    while not stop.is_set():
        try:
            reconciled = await artifacts.reconcile_all_projects(engine)
            if reconciled:
                logger.info("artifacts.reconciled", projects=reconciled)
        except Exception:
            logger.exception("artifacts.reconciler_error")
        await _sleep_or_stop(stop, interval)


async def run_secret_expiry_monitor(  # pragma: no cover — periodic orchestration
    engine: AsyncEngine, *, stop: asyncio.Event, interval: float = 6 * 60 * 60.0
) -> None:
    """Raise dashboard alerts for rotation-overdue secrets (TITAN-SEC-052).
    Deduped in the alert seam, so a periodic scan never re-notifies. Self-healing:
    a transient DB error is logged and retried."""
    while not stop.is_set():
        try:
            created = await vault_alerts.notify_overdue_secrets(engine)
            if created:
                logger.info("vault.overdue_alerts", created=created)
        except Exception:
            logger.exception("vault.expiry_monitor_error")
        await _sleep_or_stop(stop, interval)


async def run_audit_maintenance(  # pragma: no cover — live-verified daily loop
    engine: AsyncEngine,
    *,
    encrypt: "Callable[[bytes], bytes] | None",
    archive: audit_service.Archiver,
    stop: asyncio.Event,
    interval: float,
    online_days: int,
) -> None:
    """SEC-091/093: walk the audit hash chain (a break drives the intact gauge to
    0 → the AuditChainBroken page), seal past days to the age-encrypted archive,
    and prune sealed rows past the online window. Self-healing — a transient error
    is logged and retried, never propagated."""
    while not stop.is_set():
        try:
            now = datetime.now(UTC)
            broken = await audit_service.verify_chain(engine)
            AUDIT_CHAIN_INTACT.set(0.0 if broken is not None else 1.0)
            if broken is not None:
                logger.error("audit.chain_broken", broken_seq=broken)
            if encrypt is not None:
                sealed = await audit_service.seal_days(
                    engine, encrypt=encrypt, archive=archive, now=now
                )
                if sealed.events_sealed:
                    logger.info(
                        "audit.sealed", days=sealed.days_sealed, events=sealed.events_sealed
                    )
                pruned = await audit_service.apply_retention(
                    engine, now=now, online_days=online_days
                )
                if pruned:
                    logger.info("audit.retention_pruned", removed=pruned)
        except Exception:
            logger.exception("audit.maintenance_error")
        await _sleep_or_stop(stop, interval)


async def run() -> None:  # pragma: no cover — live-verified orchestration loop
    """Start the worker's stream-processing loops until interrupted."""
    settings = get_settings()
    engine = clients.create_engine(settings)
    redis = clients.create_redis(settings)

    # TITAN-SEC-020: verify signed envelopes on the worker's consume/reap path
    # and audit any drop. The worker is the dominant consumer, so signing here
    # (with the SAME key the API publishes with) is what actually enforces it.
    signer = vault_bootstrap.build_envelope_signer(settings)

    # Phase 69 (SEC-054): the worker runs its own inventory refresh + audit
    # flush — its global engine redacts every log line, tool envelope, and
    # gateway call made from THIS process, and until Phase 69 the worker held
    # no secret backend at all.
    vault_store, vault_provider = vault_bootstrap.build_secret_backend(settings)
    redaction_reporter = RedactionReporter()
    redaction_runtime.default_engine().set_reporter(redaction_reporter.record)

    async def _audit_invalid_envelope(rejection: events.InvalidEnvelope) -> None:
        await vault_service.record_envelope_rejection(
            engine,
            actor=rejection.actor,
            topic=rejection.topic,
            reason=rejection.reason,
            event_id=rejection.event_id,
            payload_digest=rejection.payload_sha256,
        )

    # TITAN-OPS-020: cap every stream at ~1M (approximate trim); the ledger, not
    # the stream, is the durability boundary.
    bus = events.build_bus(
        redis, signer=signer, on_invalid=_audit_invalid_envelope, maxlen=settings.stream_maxlen
    )
    # Phase 72: the worker reads the SAME feature-flag store the API writes. The
    # dispatch loop consults agents.pause_all every cycle; a pub/sub subscriber
    # busts the local snapshot so a flip halts dispatch within ~1 s (no restart).
    flags = flags_service.build_flag_store(
        engine, redis, ttl_seconds=settings.cache_flags_ttl_seconds
    )
    flags_subscriber = flags_service.FlagBustSubscriber(redis, flags)
    drainer = OutboxDrainer(engine, bus)
    ws_fanout = realtime.build_fanout(redis, org=settings.org)
    storage = FilesystemStorage(settings.artifact_root)
    # Memory embedding pipeline (Phase 35, Vol 5 §5.1): outbox relay → ingest
    # stream → embedder (gateway /embeddings → Qdrant) → reaper. Embeddings go
    # ONLY through the gateway (TITAN-BRN-006).
    qdrant_http = clients.create_http_client(settings)
    # SEC-082: alert Maintainers on the 80%/100% budget crossings (the worker is
    # the autonomous LLM path where caps actually trip). Injected at the root so
    # llm need not import messaging/identity.
    from titan.budget_alerts import build_budget_notifier

    llm_gateway = build_gateway(
        settings,
        engine=engine,
        redis=redis,
        budget_notifier=build_budget_notifier(engine),
        # TITAN-OPS-012: force all model traffic onto the aliased provider when
        # llm.provider_override is ON (e.g. switch to local during an incident).
        provider_override=lambda: flags.value(flags_service.PROVIDER_OVERRIDE),
    )
    # Phase 56: the real agent turn as the run executor when enabled (offline
    # acceptance run), else the deterministic placeholder (default CI/cloud).
    executor: LiveExecutor | StubExecutor
    if settings.live_executor:
        runtime = build_runtime(settings, engine, redis=redis, llm=llm_gateway, flags=flags)
        executor = LiveExecutor(runtime, make_role_resolver(engine))
    else:
        executor = StubExecutor()
    ingest_relay = memory.IngestRelay(engine, bus)
    embedder = memory.MemoryEmbedder(engine, bus, memory.QdrantIndex(qdrant_http), llm_gateway)
    # Phase 73: the audit sealer reuses the vault's age key (SEC-093). Without a
    # key (local dev) sealing is skipped — verify still runs and retention prunes
    # nothing (an unsealed row is never removed).
    audit_encrypt: Callable[[bytes], bytes] | None = None
    if settings.vault_age_key:
        _audit_recipient = vault_age.parse_identity(settings.vault_age_key).public_key()

        def _audit_seal(plaintext: bytes) -> bytes:
            return vault_age.encrypt(plaintext, _audit_recipient)

        audit_encrypt = _audit_seal
    _audit_root = Path(settings.audit_archive_root)

    def _audit_archive(ref: str, data: bytes) -> None:
        target = _audit_root / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    stop = asyncio.Event()
    consumer_name = "titan-worker-1"
    # The reaper must only reclaim GENUINELY dead workers: a live worker holds an
    # in-flight run's message for up to the soft timeout, so min-idle exceeds it.
    reaper_min_idle_ms = int((settings.run_soft_timeout_seconds + 60.0) * 1000)

    await flags_subscriber.start()  # bust local flag snapshot on cross-replica writes
    start_http_server(METRICS_PORT)  # /metrics for DLQ + stream-depth gauges
    logger.info(
        "worker.start",
        consumers=[
            "outbox-drainer",
            "domain-consumer",
            "reaper",
            "ws-fanout",
            "runs-executor",
            "runs-execute-reaper",
            "runs-results",
            "runs-results-reaper",
            "memory-ingest-relay",
            "memory-embedder",
            "memory-ingest-reaper",
            "pipeline-reconciler",
            "clarification-timeout-reaper",
        ],
        worker_concurrency=settings.worker_concurrency,
        metrics_port=METRICS_PORT,
    )
    tasks = [
        asyncio.create_task(
            run_drainer(drainer, stop=stop, idle_seconds=settings.outbox_drain_idle_seconds)
        ),
        asyncio.create_task(
            run_domain_consumer(engine, bus, consumer_name, stop=stop, redis=redis)
        ),
        asyncio.create_task(run_reaper(engine, bus, consumer_name, stop=stop, redis=redis)),
        asyncio.create_task(
            realtime.run_ws_fanout(bus, ws_fanout, f"{consumer_name}-ws", stop=stop)
        ),
        asyncio.create_task(
            run_executor_loop(
                engine,
                bus,
                storage,
                executor,
                consumer_name,
                stop=stop,
                concurrency=settings.worker_concurrency,
                soft_timeout=settings.run_soft_timeout_seconds,
                # TITAN-OPS-012 kill switch: halt new dispatch across all workers
                # within 15 s when agents.pause_all is flipped (no restart).
                paused=lambda: flags.is_enabled(flags_service.PAUSE_ALL),
            )
        ),
        asyncio.create_task(
            run_execute_reaper(
                engine,
                bus,
                storage,
                executor,
                consumer_name,
                stop=stop,
                soft_timeout=settings.run_soft_timeout_seconds,
                min_idle_ms=reaper_min_idle_ms,
                # TITAN-OPS-012: pause_all halts reclaim/re-exec too, not just new dispatch.
                paused=lambda: flags.is_enabled(flags_service.PAUSE_ALL),
            )
        ),
        asyncio.create_task(run_results_loop(engine, bus, consumer_name, stop=stop)),
        asyncio.create_task(run_results_reaper(engine, bus, consumer_name, stop=stop)),
        asyncio.create_task(memory.run_ingest_relay(ingest_relay, stop=stop)),
        asyncio.create_task(memory.run_embedder(embedder, bus, consumer_name, stop=stop)),
        asyncio.create_task(memory.run_ingest_reaper(embedder, bus, consumer_name, stop=stop)),
        asyncio.create_task(run_pipeline_reconciler(engine, stop=stop)),
        asyncio.create_task(run_clarification_timeout_reaper(engine, stop=stop)),
        asyncio.create_task(run_blocker_sweeper(engine, stop=stop)),
        asyncio.create_task(run_artifact_reconciler(engine, stop=stop)),
        asyncio.create_task(run_secret_expiry_monitor(engine, stop=stop)),
        asyncio.create_task(
            run_audit_maintenance(
                engine,
                encrypt=audit_encrypt,
                archive=_audit_archive,
                stop=stop,
                interval=settings.audit_maintenance_interval_seconds,
                online_days=settings.audit_online_days,
            )
        ),
        asyncio.create_task(
            vault_inventory.run_redaction_maintenance(
                engine,
                vault_provider,
                vault_store,
                redaction_reporter,
                service="titan-worker",
                stop=stop,
            )
        ),
    ]
    try:
        # return_exceptions: a loop that somehow escapes its own guard cannot
        # cancel its siblings; we log and keep the rest running.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("worker.loop_exited", error=str(result))
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)
        await flags_subscriber.stop()
        await llm_gateway.aclose()
        await qdrant_http.aclose()
        await redis.aclose()
        await engine.dispose()


def main() -> None:  # pragma: no cover — process entrypoint
    """Process entrypoint for ``python -m titan.worker``."""
    configure_logging(get_settings().log_level, service="titan-worker")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
