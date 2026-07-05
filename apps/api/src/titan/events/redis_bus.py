"""Redis Streams implementation of QueueBus (ADR-BCK-02, TITAN-BCK-010/011).

XADD (MAXLEN ~ 100000) → XREADGROUP + explicit XACK; a reaper runs XAUTOCLAIM
(min-idle 60 s) to reclaim work from dead consumers; poison messages move to
``stream:{name}.dlq`` after max_attempts deliveries (tracked via the XPENDING
delivery counter). Consumers dedup on event_id via per-event ``seen:{group}:``
keys with 24 h TTL.
"""

import datetime as dt
from typing import Any, cast

import structlog
import uuid_utils
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from titan.events.bus import Delivery
from titan.events.envelope import Envelope
from titan.events.streams import dlq_stream
from titan.observability.metrics import STREAM_DLQ_DEPTH

logger = structlog.get_logger("titan.events")

_WIRE_FIELD = "data"
SEEN_TTL_SECONDS = 24 * 60 * 60


class RedisStreamBus:
    """QueueBus over Redis Streams. One instance per process; safe to share."""

    def __init__(self, redis: Redis, *, maxlen: int = 100_000) -> None:
        self._redis = redis
        self._maxlen = maxlen

    async def ensure_group(self, topic: str, group: str) -> None:
        try:
            # MKSTREAM so the group can be created before the first publish.
            await self._redis.xgroup_create(topic, group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

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
        envelope = Envelope(
            event_id=event_id or str(uuid_utils.uuid7()),
            topic=topic,
            occurred_at=occurred_at or dt.datetime.now(dt.UTC),
            actor=actor,
            payload=payload,
            trace_id=trace_id,
        )
        await self._redis.xadd(
            topic, {_WIRE_FIELD: envelope.to_wire()}, maxlen=self._maxlen, approximate=True
        )
        return envelope.event_id

    async def consume(
        self, topic: str, group: str, consumer: str, *, count: int = 10, block_ms: int = 5000
    ) -> list[Delivery]:
        response = await self._redis.xreadgroup(
            group, consumer, {topic: ">"}, count=count, block=block_ms
        )
        deliveries: list[Delivery] = []
        for _stream, entries in cast("list[tuple[str, list[Any]]]", response or []):
            for entry_id, fields in cast("list[tuple[str, dict[str, str]]]", entries):
                envelope = Envelope.from_wire(fields[_WIRE_FIELD])
                if await self._already_seen(group, envelope.event_id):
                    # Duplicate: ack and drop — no side effect (TITAN-BCK-010).
                    await self._redis.xack(topic, group, entry_id)
                    continue
                deliveries.append(Delivery(handle=entry_id, envelope=envelope, delivery_count=1))
        return deliveries

    async def ack(self, topic: str, group: str, delivery: Delivery) -> None:
        await self._redis.xack(topic, group, delivery.handle)
        await self._mark_seen(group, delivery.envelope.event_id)

    async def nack(self, topic: str, group: str, delivery: Delivery) -> None:
        # Leave pending (do not XACK); the reaper reclaims it after min-idle.
        return None

    async def reap(
        self,
        topic: str,
        group: str,
        consumer: str,
        *,
        min_idle_ms: int = 60_000,
        max_attempts: int = 3,
    ) -> list[Delivery]:
        _cursor, claimed, _deleted = await self._redis.xautoclaim(
            topic, group, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=100
        )
        deliveries: list[Delivery] = []
        for entry_id, fields in cast("list[tuple[str, dict[str, str]]]", claimed):
            envelope = Envelope.from_wire(fields[_WIRE_FIELD])
            if await self._already_seen(group, envelope.event_id):
                await self._redis.xack(topic, group, entry_id)
                continue
            times = await self._delivery_count(topic, group, entry_id)
            if times > max_attempts:
                await self._to_dlq(topic, group, entry_id, envelope, times)
                continue
            deliveries.append(Delivery(handle=entry_id, envelope=envelope, delivery_count=times))
        return deliveries

    async def pending_depth(self, topic: str, group: str) -> int:
        summary = await self._redis.xpending(topic, group)
        return int(cast(Any, summary)["pending"]) if summary else 0

    # -- internals ---------------------------------------------------------

    def _seen_key(self, group: str, event_id: str) -> str:
        # Per-event key (not one big set): gives each event its own 24 h TTL.
        return f"seen:{group}:{event_id}"

    async def _already_seen(self, group: str, event_id: str) -> bool:
        return bool(await self._redis.exists(self._seen_key(group, event_id)))

    async def _mark_seen(self, group: str, event_id: str) -> None:
        await self._redis.set(self._seen_key(group, event_id), "1", ex=SEEN_TTL_SECONDS)

    async def _delivery_count(self, topic: str, group: str, entry_id: str) -> int:
        pending = await self._redis.xpending_range(
            topic, group, min=entry_id, max=entry_id, count=1
        )
        if not pending:
            return 1
        return int(cast(Any, pending[0])["times_delivered"])

    async def _to_dlq(
        self, topic: str, group: str, entry_id: str, envelope: Envelope, times: int
    ) -> None:
        dlq = dlq_stream(topic)
        await self._redis.xadd(
            dlq,
            {
                _WIRE_FIELD: envelope.to_wire(),
                "origin_stream": topic,
                "origin_id": entry_id,
                "delivery_count": str(times),
            },
            maxlen=self._maxlen,
            approximate=True,
        )
        await self._redis.xack(topic, group, entry_id)
        depth = await self._redis.xlen(dlq)
        STREAM_DLQ_DEPTH.labels(stream=topic).set(depth)
        logger.warning(
            "queue.poison_message",
            stream=topic,
            event_id=envelope.event_id,
            delivery_count=times,
        )
