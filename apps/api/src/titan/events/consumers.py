"""The demo domain consumer + the worker's stream-processing loops.

The demo consumer of ``stream:events.domain`` proves the full outbox → queue →
consumer loop and is idempotent via the event_inbox ledger: replaying the
stream produces no duplicate side effects (TITAN-BCK-010).

Every loop is independently resilient: a transient Redis/DB error is logged
and backed off, never propagated — one failing loop must not tear the worker
down (a routine Redis restart must not halt the events pipeline).
"""

import asyncio
import contextlib

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.events.bus import Delivery, QueueBus
from titan.events.streams import STREAM_EVENTS_DOMAIN
from titan.observability.metrics import STREAM_DEPTH

logger = structlog.get_logger("titan.events")

DOMAIN_GROUP = "titan-domain"


async def record_inbox(engine: AsyncEngine, group: str, delivery: Delivery) -> bool:
    """Idempotent side effect: insert into event_inbox ON CONFLICT DO NOTHING.
    Returns True if this delivery was new (row inserted), False on replay.

    The ledger is keyed on event_id alone (global idempotency) so replaying a
    stream into a fresh group is a no-op (TITAN-BCK-010 acceptance). A future
    consumer that needs genuine per-group fan-out must key its OWN side effect
    per (event_id, group) rather than reuse this demo ledger."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO event_inbox (event_id, consumer_group, topic) "
                "VALUES (:eid, :grp, :topic) ON CONFLICT DO NOTHING"
            ),
            {
                "eid": delivery.envelope.event_id,
                "grp": group,
                "topic": delivery.envelope.topic,
            },
        )
        return result.rowcount == 1


async def handle_domain_delivery(engine: AsyncEngine, bus: QueueBus, delivery: Delivery) -> None:
    """Process one domain delivery: idempotent record + ack; nack on failure so
    the reaper retries it (and eventually dead-letters genuine poison). Shared
    by the live consume path AND the reaper's reprocessing path."""
    try:
        inserted = await record_inbox(engine, DOMAIN_GROUP, delivery)
        await bus.ack(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP, delivery)
        if inserted:
            logger.info("events.consumed", event_id=delivery.envelope.event_id)
    except Exception:
        logger.exception("events.consume_failed", event_id=delivery.envelope.event_id)
        await bus.nack(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP, delivery)


async def process_domain_batch(
    engine: AsyncEngine, bus: QueueBus, consumer: str, *, block_ms: int = 1000
) -> int:
    """Consume one batch of NEW messages and handle each; returns the count.
    The deterministic unit the loop and the tests both call."""
    deliveries = await bus.consume(
        STREAM_EVENTS_DOMAIN, DOMAIN_GROUP, consumer, count=50, block_ms=block_ms
    )
    for delivery in deliveries:
        await handle_domain_delivery(engine, bus, delivery)
    return len(deliveries)


async def reap_domain_once(engine: AsyncEngine, bus: QueueBus, consumer: str) -> int:
    """Reclaim idle-pending domain messages and REPROCESS them (retry), routing
    genuine poison to the DLQ. Returns how many were reclaimed. Also publishes
    the pending-depth gauge for the domain group."""
    reclaimed = await bus.reap(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP, consumer, min_idle_ms=60_000)
    for delivery in reclaimed:
        await handle_domain_delivery(engine, bus, delivery)
    STREAM_DEPTH.labels(stream=STREAM_EVENTS_DOMAIN, group=DOMAIN_GROUP).set(
        await bus.pending_depth(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP)
    )
    return len(reclaimed)


# The three run_* loops below are long-running orchestration whose per-iteration
# units (process_domain_batch, reap_domain_once, drainer.drain_once) are
# unit-tested; the loops are verified end-to-end by the live Compose worker
# (the phase DoD). Each swallows transient errors so it self-heals — hence
# pragma: no cover.


async def run_domain_consumer(  # pragma: no cover
    engine: AsyncEngine, bus: QueueBus, consumer: str, *, stop: asyncio.Event
) -> None:
    """Consume stream:events.domain into the inbox ledger until stopped."""
    await bus.ensure_group(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP)
    while not stop.is_set():
        try:
            processed = await process_domain_batch(engine, bus, consumer)
        except Exception:
            logger.exception("events.consumer_loop_error")
            await _sleep_or_stop(stop, 2.0)
            continue
        if not processed:
            await _sleep_or_stop(stop, 0.5)


async def run_reaper(  # pragma: no cover
    engine: AsyncEngine, bus: QueueBus, consumer: str, *, stop: asyncio.Event
) -> None:
    """Reclaim + reprocess idle-pending domain work every 30 s (min-idle 60 s)."""
    await bus.ensure_group(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP)
    while not stop.is_set():
        try:
            await reap_domain_once(engine, bus, consumer)
        except Exception:
            logger.exception("events.reaper_loop_error")
        await _sleep_or_stop(stop, 30.0)


async def run_drainer(drainer: object, *, stop: asyncio.Event) -> None:  # pragma: no cover
    """Continuously drain the transactional outbox to the queue."""
    from titan.events.outbox import OutboxDrainer

    assert isinstance(drainer, OutboxDrainer)
    while not stop.is_set():
        try:
            published = await drainer.drain_once()
        except Exception:
            logger.exception("events.drainer_loop_error")
            await _sleep_or_stop(stop, 2.0)
            continue
        if not published:
            await _sleep_or_stop(stop, 1.0)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:  # pragma: no cover
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
