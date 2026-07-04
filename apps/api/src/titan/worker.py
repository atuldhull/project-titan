"""Stream-consumer entrypoint for titan-worker (Vol 3 §2.1, TITAN-BCK-003).

Runs from the same image as titan-core with entrypoint ``python -m titan.worker``.
Redis Streams consumer groups are registered by later phases; this entrypoint
starts, reports an empty consumer registry, and exits cleanly.
"""

import asyncio
import logging

logger = logging.getLogger("titan.worker")


async def run() -> None:
    """Start the worker loop over all registered stream consumers."""
    consumers: list[str] = []
    logger.info("titan-worker starting: %d stream consumers registered", len(consumers))


def main() -> None:
    """Process entrypoint for ``python -m titan.worker``."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
