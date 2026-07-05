"""Stream-consumer entrypoint for titan-worker (Vol 3 §2.1, TITAN-BCK-003).

Runs from the same image as titan-core with entrypoint ``python -m titan.worker``.
At this phase the worker runs the events backbone: the transactional-outbox
drainer, the demo consumer of stream:events.domain, and the reaper that
reclaims-and-reprocesses idle-pending work and dead-letters poison messages.

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
    stop = asyncio.Event()
    consumer_name = "titan-worker-1"

    start_http_server(METRICS_PORT)  # /metrics for DLQ + stream-depth gauges
    logger.info(
        "worker.start",
        consumers=["outbox-drainer", "domain-consumer", "reaper"],
        metrics_port=METRICS_PORT,
    )
    tasks = [
        asyncio.create_task(run_drainer(drainer, stop=stop)),
        asyncio.create_task(run_domain_consumer(engine, bus, consumer_name, stop=stop)),
        asyncio.create_task(run_reaper(engine, bus, consumer_name, stop=stop)),
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
