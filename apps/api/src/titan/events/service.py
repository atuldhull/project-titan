"""Public surface of the `events` library module (Vol 3 §3).

Re-exports the queue backbone every other module publishes/consumes through:
the broker-agnostic QueueBus (ADR-BCK-02), the transactional outbox
(TITAN-BCK-005), and the envelope contract (TITAN-BCK-010).
"""

from typing import TYPE_CHECKING

from redis.asyncio import Redis

from titan.events.bus import (
    Delivery,
    InvalidEnvelope,
    InvalidEnvelopeHook,
    QueueBus,
    Signer,
)

if TYPE_CHECKING:
    from titan.events.nats_bus import NatsStreamBus
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
    maxlen: int | None = None,
) -> QueueBus:
    """Construct the QueueBus. The backend is Redis Streams; a future NATS
    JetStream implementation (ADR-BCK-02) swaps in behind this same Protocol
    with identical envelopes, but will need its own factory signature (it
    takes connection config, not a Redis client) and a QUEUE_BACKEND setting —
    neither exists yet.

    ``signer`` enables TITAN-SEC-020 envelope signing: publish signs, consume/
    reap verify and DROP + audit (via ``on_invalid``) a bad/missing signature.
    Both are injected here so ``titan.events`` never imports the vault.

    ``maxlen`` caps every stream's XADD MAXLEN ~N (approximate trim, TITAN-OPS-020,
    default ~1M in production via settings.stream_maxlen); None keeps the
    RedisStreamBus default (100k) for tests/zero-config."""
    if maxlen is not None:
        return RedisStreamBus(redis, maxlen=maxlen, signer=signer, on_invalid=on_invalid)
    return RedisStreamBus(redis, signer=signer, on_invalid=on_invalid)


async def connect_bus(
    *,
    backend: str,
    redis: Redis,
    nats_url: str | None = None,
    signer: Signer | None = None,
    on_invalid: InvalidEnvelopeHook | None = None,
    maxlen: int | None = None,
) -> QueueBus:
    """Select the QueueBus backend from config (FND-051 config-only swap). "redis"
    (default) returns the Redis Streams bus over the given client; "nats" connects to
    JetStream. Takes primitives (not a Settings object) so ``titan.events`` stays a leaf.
    A NATS bus owns its connection — the composition root closes it via ``bus.aclose()``."""
    if backend == "nats":
        if not nats_url:
            raise ValueError("QUEUE_BACKEND=nats requires nats_url")
        return await connect_nats_bus(nats_url, signer=signer, on_invalid=on_invalid, maxlen=maxlen)
    if backend != "redis":
        raise ValueError(f"unknown QUEUE_BACKEND '{backend}' (expected 'redis' or 'nats')")
    return build_bus(redis, signer=signer, on_invalid=on_invalid, maxlen=maxlen)


async def connect_nats_bus(
    url: str,
    *,
    signer: Signer | None = None,
    on_invalid: InvalidEnvelopeHook | None = None,
    maxlen: int | None = None,
) -> "NatsStreamBus":
    """Connect to NATS JetStream and return the QueueBus (ADR-005 / FND-051), the
    scale-out backend selected by QUEUE_BACKEND=nats. The returned bus OWNS the
    connection; the composition root must call ``await bus.aclose()`` at shutdown.
    Imported lazily so a Redis-only deployment never needs nats-py at import time."""
    import nats

    from titan.events.nats_bus import NatsStreamBus

    nc = await nats.connect(url)
    return NatsStreamBus(
        nc.jetstream(),
        nc=nc,
        max_msgs=maxlen or 100_000,
        signer=signer,
        on_invalid=on_invalid,
    )


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
    "connect_bus",
    "connect_nats_bus",
    "dlq_stream",
    "write_event",
]
