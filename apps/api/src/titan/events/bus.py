"""Broker-agnostic queue interface (ADR-BCK-02, ADR-005).

The QueueBus signatures use only str / dict / Envelope — no Redis types — so
a QUEUE_BACKEND=nats JetStream implementation can swap in with identical
envelopes and no domain-code changes. The Redis Streams implementation lives
in titan.events.redis_bus.
"""

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from titan.events.envelope import Envelope

# Invoked by reap() for each envelope it routes to the DLQ, so the caller can
# make dead-lettering terminal in ITS canonical store (e.g. stamp the source
# outbox row) — otherwise a stale-republish loop would resurrect the poison.
DeadLetterHook = Callable[[Envelope], Awaitable[None]]


@dataclass
class Delivery:
    """One consumed message. ``handle`` is a broker-opaque ack token (a Redis
    stream entry id today), ``delivery_count`` is how many times the broker
    has delivered it (for poison detection)."""

    handle: str
    envelope: Envelope
    delivery_count: int


class QueueBus(Protocol):
    async def ensure_group(self, topic: str, group: str) -> None:
        """Create the consumer group for a stream (idempotent)."""
        ...

    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        actor: str,
        trace_id: str | None = None,
        event_id: str | None = None,
        occurred_at: dt.datetime | None = None,
    ) -> str:
        """Publish an event; returns its event_id. Passing ``event_id`` (e.g.
        an outbox row id) makes re-publishing idempotent at the consumer."""
        ...

    async def consume(
        self, topic: str, group: str, consumer: str, *, count: int = 10, block_ms: int = 5000
    ) -> list[Delivery]:
        """Read new messages for a consumer; dedup'd, poison-free."""
        ...

    async def ack(self, topic: str, group: str, delivery: Delivery) -> None:
        """Mark a delivery processed (and record its event_id as seen)."""
        ...

    async def nack(self, topic: str, group: str, delivery: Delivery) -> None:
        """Leave a delivery pending for the reaper to reclaim."""
        ...

    async def reap(
        self,
        topic: str,
        group: str,
        consumer: str,
        *,
        min_idle_ms: int = 60_000,
        max_attempts: int = 3,
        on_dead: DeadLetterHook | None = None,
    ) -> list[Delivery]:
        """Reclaim idle-pending messages from dead consumers; route poison
        (over max_attempts deliveries) to the DLQ, invoking ``on_dead`` per
        dead-lettered envelope so the producer's canonical store can stop
        republishing it. Callers MUST reprocess the returned deliveries
        (retry) — discarding them breaks at-least-once."""
        ...

    async def pending_depth(self, topic: str, group: str) -> int:
        """Number of pending (delivered, unacked) messages for a group —
        the backpressure signal exported as titan_stream_depth."""
        ...
