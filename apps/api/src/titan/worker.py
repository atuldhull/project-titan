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

from titan import clients
from titan.events import service as events
from titan.events.consumers import run_domain_consumer, run_drainer, run_reaper
from titan.events.outbox import OutboxDrainer
from titan.observability.logging import configure_logging
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


async def run() -> None:  # pragma: no cover — live-verified orchestration loop
    """Start the worker's stream-processing loops until interrupted."""
    settings = get_settings()
    engine = clients.create_engine(settings)
    redis = clients.create_redis(settings)
    bus = events.build_bus(redis)
    drainer = OutboxDrainer(engine, bus)
    storage = FilesystemStorage(settings.artifact_root)
    executor = StubExecutor()
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
            "runs-executor",
            "runs-execute-reaper",
            "runs-results",
            "runs-results-reaper",
        ],
        worker_concurrency=settings.worker_concurrency,
        metrics_port=METRICS_PORT,
    )
    tasks = [
        asyncio.create_task(run_drainer(drainer, stop=stop)),
        asyncio.create_task(run_domain_consumer(engine, bus, consumer_name, stop=stop)),
        asyncio.create_task(run_reaper(engine, bus, consumer_name, stop=stop)),
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
        await redis.aclose()
        await engine.dispose()


def main() -> None:  # pragma: no cover — process entrypoint
    """Process entrypoint for ``python -m titan.worker``."""
    configure_logging(get_settings().log_level, service="titan-worker")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
