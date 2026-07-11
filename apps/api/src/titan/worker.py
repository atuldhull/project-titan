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

import structlog
from prometheus_client import start_http_server
from sqlalchemy.ext.asyncio import AsyncEngine

from titan import clients
from titan.events import service as events
from titan.events.consumers import run_domain_consumer, run_drainer, run_reaper
from titan.events.outbox import OutboxDrainer
from titan.llm.service import build_gateway
from titan.memory import service as memory
from titan.observability.logging import configure_logging
from titan.orchestrator import service as orchestrator
from titan.realtime import service as realtime
from titan.runs.coordinator import (
    run_execute_reaper,
    run_executor_loop,
    run_results_loop,
    run_results_reaper,
)
from titan.runs.executor import StubExecutor
from titan.runs.storage import FilesystemStorage
from titan.settings import get_settings

logger = structlog.get_logger("titan.worker")

METRICS_PORT = 9100

# How often the orchestrator seeds pipelines for projects created while it was
# down. In-flight pipelines resume structurally (their cursor lives in Postgres),
# so this only needs to be brisk enough to satisfy the "replacement resumes within
# 30 seconds" DoD for projects with no pipeline yet.
PIPELINE_RECONCILE_INTERVAL_S = 15.0


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


async def run() -> None:  # pragma: no cover — live-verified orchestration loop
    """Start the worker's stream-processing loops until interrupted."""
    settings = get_settings()
    engine = clients.create_engine(settings)
    redis = clients.create_redis(settings)
    bus = events.build_bus(redis)
    drainer = OutboxDrainer(engine, bus)
    ws_fanout = realtime.build_fanout(redis, org=settings.org)
    storage = FilesystemStorage(settings.artifact_root)
    executor = StubExecutor()
    # Memory embedding pipeline (Phase 35, Vol 5 §5.1): outbox relay → ingest
    # stream → embedder (gateway /embeddings → Qdrant) → reaper. Embeddings go
    # ONLY through the gateway (TITAN-BRN-006).
    qdrant_http = clients.create_http_client(settings)
    llm_gateway = build_gateway(settings, engine=engine, redis=redis)
    ingest_relay = memory.IngestRelay(engine, bus)
    embedder = memory.MemoryEmbedder(engine, bus, memory.QdrantIndex(qdrant_http), llm_gateway)
    stop = asyncio.Event()
    consumer_name = "titan-worker-1"
    # The reaper must only reclaim GENUINELY dead workers: a live worker holds an
    # in-flight run's message for up to the soft timeout, so min-idle exceeds it.
    reaper_min_idle_ms = int((settings.run_soft_timeout_seconds + 60.0) * 1000)

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
        ],
        worker_concurrency=settings.worker_concurrency,
        metrics_port=METRICS_PORT,
    )
    tasks = [
        asyncio.create_task(
            run_drainer(drainer, stop=stop, idle_seconds=settings.outbox_drain_idle_seconds)
        ),
        asyncio.create_task(run_domain_consumer(engine, bus, consumer_name, stop=stop)),
        asyncio.create_task(run_reaper(engine, bus, consumer_name, stop=stop)),
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
            )
        ),
        asyncio.create_task(run_results_loop(engine, bus, consumer_name, stop=stop)),
        asyncio.create_task(run_results_reaper(engine, bus, consumer_name, stop=stop)),
        asyncio.create_task(memory.run_ingest_relay(ingest_relay, stop=stop)),
        asyncio.create_task(memory.run_embedder(embedder, bus, consumer_name, stop=stop)),
        asyncio.create_task(memory.run_ingest_reaper(embedder, bus, consumer_name, stop=stop)),
        asyncio.create_task(run_pipeline_reconciler(engine, stop=stop)),
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
