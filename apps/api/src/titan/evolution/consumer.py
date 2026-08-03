"""The evolution.scorecard consumer (Phase 84, TITAN-EVO-004).

A fan-out consumer group on ``stream:events.domain`` that reacts to the ONE
``task.terminal`` event the task state machine emits, generating a scorecard
off-transaction (within the 60 s DoD budget). It is NOT a second completion
listener — it consumes the single terminal event the transition already emits.

Generation is idempotent (``generate_scorecard`` skips a task that already has a
scorecard) and never raises past a logged failure, so a redelivery or a wonky
evaluator can't wedge the group.
"""

import asyncio
import contextlib
import uuid
from typing import Any

import structlog

from titan.events.bus import Delivery, QueueBus
from titan.events.streams import STREAM_EVENTS_DOMAIN
from titan.evolution import scoring
from titan.evolution.resolver import EvolutionResolver
from titan.observability.metrics import STREAM_DEPTH

logger = structlog.get_logger("titan.evolution.consumer")

SCORECARD_GROUP = "evolution-scorecard"


def _terminal_task_id(delivery: Delivery) -> uuid.UUID | None:
    payload = delivery.envelope.payload
    if payload.get("type") != "task.terminal":
        return None
    task = payload.get("task")
    if not isinstance(task, dict):
        return None
    raw = task.get("id")
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def handle_scorecard_delivery(
    engine: Any, bus: QueueBus, gateway: Any, resolver: EvolutionResolver, delivery: Delivery
) -> None:
    """Generate a scorecard for a task.terminal delivery, then ack. Non-terminal
    events are acked untouched (this group fans out over the whole domain stream)."""
    try:
        task_id = _terminal_task_id(delivery)
        if task_id is not None:
            await scoring.generate_scorecard(engine, gateway, resolver, task_id=task_id)
        await bus.ack(STREAM_EVENTS_DOMAIN, SCORECARD_GROUP, delivery)
    except Exception:
        logger.exception("scorecard.consume_failed", event_id=delivery.envelope.event_id)
        await bus.nack(STREAM_EVENTS_DOMAIN, SCORECARD_GROUP, delivery)


async def process_scorecard_batch(
    engine: Any,
    bus: QueueBus,
    gateway: Any,
    resolver: EvolutionResolver,
    consumer: str,
    *,
    block_ms: int = 1000,
) -> int:
    deliveries = await bus.consume(
        STREAM_EVENTS_DOMAIN, SCORECARD_GROUP, consumer, count=50, block_ms=block_ms
    )
    for delivery in deliveries:
        await handle_scorecard_delivery(engine, bus, gateway, resolver, delivery)
    return len(deliveries)


async def reap_scorecard_once(
    engine: Any, bus: QueueBus, gateway: Any, resolver: EvolutionResolver, consumer: str
) -> int:
    reclaimed = await bus.reap(STREAM_EVENTS_DOMAIN, SCORECARD_GROUP, consumer, min_idle_ms=60_000)
    for delivery in reclaimed:
        await handle_scorecard_delivery(engine, bus, gateway, resolver, delivery)
    STREAM_DEPTH.labels(stream=STREAM_EVENTS_DOMAIN, group=SCORECARD_GROUP).set(
        await bus.pending_depth(STREAM_EVENTS_DOMAIN, SCORECARD_GROUP)
    )
    return len(reclaimed)


async def run_scorecard_consumer(  # pragma: no cover - loop verified via its units
    engine: Any,
    bus: QueueBus,
    gateway: Any,
    resolver: EvolutionResolver,
    consumer: str,
    *,
    stop: asyncio.Event,
) -> None:
    await bus.ensure_group(STREAM_EVENTS_DOMAIN, SCORECARD_GROUP)
    while not stop.is_set():
        try:
            processed = await process_scorecard_batch(engine, bus, gateway, resolver, consumer)
        except Exception:
            logger.exception("scorecard.consumer_loop_error")
            await _sleep_or_stop(stop, 2.0)
            continue
        if not processed:
            await _sleep_or_stop(stop, 0.5)


async def run_scorecard_reaper(  # pragma: no cover - loop verified via its units
    engine: Any,
    bus: QueueBus,
    gateway: Any,
    resolver: EvolutionResolver,
    consumer: str,
    *,
    stop: asyncio.Event,
) -> None:
    await bus.ensure_group(STREAM_EVENTS_DOMAIN, SCORECARD_GROUP)
    while not stop.is_set():
        try:
            await reap_scorecard_once(engine, bus, gateway, resolver, consumer)
        except Exception:
            logger.exception("scorecard.reaper_loop_error")
        await _sleep_or_stop(stop, 30.0)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:  # pragma: no cover
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


__all__ = [
    "SCORECARD_GROUP",
    "handle_scorecard_delivery",
    "process_scorecard_batch",
    "reap_scorecard_once",
    "run_scorecard_consumer",
    "run_scorecard_reaper",
]
