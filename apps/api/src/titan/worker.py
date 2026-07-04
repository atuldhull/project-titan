"""Stream-consumer entrypoint for titan-worker (Vol 3 §2.1, TITAN-BCK-003).

Runs from the same image as titan-core with entrypoint ``python -m titan.worker``.
Redis Streams consumer groups are registered by later phases; this entrypoint
starts with structured logging, reports an empty consumer registry, and exits
cleanly.
"""

import asyncio

import structlog

from titan.observability.logging import configure_logging
from titan.settings import get_settings

logger = structlog.get_logger("titan.worker")


async def run() -> None:
    """Start the worker loop over all registered stream consumers."""
    consumers: list[str] = []
    logger.info("worker.start", registered_consumers=len(consumers))


def main() -> None:
    """Process entrypoint for ``python -m titan.worker``."""
    configure_logging(get_settings().log_level)
    asyncio.run(run())


if __name__ == "__main__":
    main()
