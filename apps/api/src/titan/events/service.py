"""Public surface of the `events` library module (Vol 3 §3).

Re-exports the queue backbone every other module publishes/consumes through:
the broker-agnostic QueueBus (ADR-BCK-02), the transactional outbox
(TITAN-BCK-005), and the envelope contract (TITAN-BCK-010).
"""

from redis.asyncio import Redis

from titan.events.bus import (
    Delivery,
    InvalidEnvelope,
    InvalidEnvelopeHook,
    QueueBus,
    Signer,
)
from titan.events.envelope import Envelope
from titan.events.outbox import OutboxDrainer, write_event
from titan.events.redis_bus import RedisStreamBus
from titan.events.streams import (
    ALL_STREAMS,
    STREAM_EVENTS_DOMAIN,
    STREAM_RUNS_EXECUTE,
    STREAM_RUNS_RESULTS,
    STREAM_TASKS_READY,
    dlq_stream,
)


def build_bus(
    redis: Redis,
    *,
    signer: Signer | None = None,
    on_invalid: InvalidEnvelopeHook | None = None,
) -> QueueBus:
    """Construct the QueueBus. The backend is Redis Streams; a future NATS
    JetStream implementation (ADR-BCK-02) swaps in behind this same Protocol
    with identical envelopes, but will need its own factory signature (it
    takes connection config, not a Redis client) and a QUEUE_BACKEND setting —
    neither exists yet.

    ``signer`` enables TITAN-SEC-020 envelope signing: publish signs, consume/
    reap verify and DROP + audit (via ``on_invalid``) a bad/missing signature.
    Both are injected here so ``titan.events`` never imports the vault."""
    return RedisStreamBus(redis, signer=signer, on_invalid=on_invalid)


__all__ = [
    "ALL_STREAMS",
    "STREAM_EVENTS_DOMAIN",
    "STREAM_RUNS_EXECUTE",
    "STREAM_RUNS_RESULTS",
    "STREAM_TASKS_READY",
    "Delivery",
    "Envelope",
    "InvalidEnvelope",
    "InvalidEnvelopeHook",
    "OutboxDrainer",
    "QueueBus",
    "RedisStreamBus",
    "Signer",
    "build_bus",
    "dlq_stream",
    "write_event",
]
