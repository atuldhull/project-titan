"""Redis Streams implementation of QueueBus (ADR-BCK-02, TITAN-BCK-010/011).

XADD (MAXLEN ~ 100000) → XREADGROUP + explicit XACK; a reaper runs XAUTOCLAIM
(min-idle 60 s) to reclaim work from dead consumers; poison messages move to
``stream:{name}.dlq`` after max_attempts deliveries (tracked via the XPENDING
delivery counter). Consumers dedup on event_id via per-event ``seen:{group}:``
keys with 24 h TTL.
"""

import datetime as dt
import hashlib
from typing import Any, cast

import structlog
import uuid_utils
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from titan.events.bus import (
    DeadLetterHook,
    Delivery,
    InvalidEnvelope,
    InvalidEnvelopeHook,
    Signer,
)
from titan.events.envelope import Envelope
from titan.events.streams import dlq_stream
from titan.observability.metrics import STREAM_DLQ_DEPTH

logger = structlog.get_logger("titan.events")

_WIRE_FIELD = "data"
_WIRE_SIG = "sig"  # TITAN-SEC-020 HMAC over the _WIRE_FIELD bytes
SEEN_TTL_SECONDS = 24 * 60 * 60


class RedisStreamBus:
    """QueueBus over Redis Streams. One instance per process; safe to share.

    When a ``signer`` is set (TITAN-SEC-020), publish attaches an HMAC over the
    serialized envelope and consume/reap verify it: a bad, missing, or
    unparseable envelope is DROPPED (acked, never processed) and reported to
    ``on_invalid`` for the audit event — it is NOT raised, because raising before
    the poison check is exactly what would wedge the reaper on a single bad
    frame."""

    def __init__(
        self,
        redis: Redis,
        *,
        maxlen: int = 100_000,
        signer: Signer | None = None,
        on_invalid: InvalidEnvelopeHook | None = None,
    ) -> None:
        self._redis = redis
        self._maxlen = maxlen
        self._signer = signer
        self._on_invalid = on_invalid

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
        wire = envelope.to_wire()
        # Two literal branches (not a built variable) so mypy contextually types
        # the mapping against xadd's expected field type.
        if self._signer is not None:
            await self._redis.xadd(
                topic,
                {_WIRE_FIELD: wire, _WIRE_SIG: self._signer.sign(wire)},
                maxlen=self._maxlen,
                approximate=True,
            )
        else:
            await self._redis.xadd(
                topic, {_WIRE_FIELD: wire}, maxlen=self._maxlen, approximate=True
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
                envelope = await self._verify_and_parse(topic, group, entry_id, fields)
                if envelope is None:
                    continue  # dropped + audited (bad/missing signature or unparseable)
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
        on_dead: DeadLetterHook | None = None,
    ) -> list[Delivery]:
        _cursor, claimed, _deleted = await self._redis.xautoclaim(
            topic, group, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=100
        )
        deliveries: list[Delivery] = []
        for entry_id, fields in cast("list[tuple[str, dict[str, str]]]", claimed):
            envelope = await self._verify_and_parse(topic, group, entry_id, fields)
            if envelope is None:
                continue  # dropped + audited — never counts against max_attempts
            if await self._already_seen(group, envelope.event_id):
                await self._redis.xack(topic, group, entry_id)
                continue
            times = await self._delivery_count(topic, group, entry_id)
            if times > max_attempts:
                if on_dead is not None:
                    # Let the producer's canonical store record the quarantine —
                    # e.g. stamp its outbox row so a stale-republish loop cannot
                    # resurrect the poison and refill the DLQ forever. Invoked
                    # BEFORE _to_dlq: a crash between the two leaves the entry
                    # in the PEL with the stamp durable, and the next reap
                    # re-detects it. A failing hook must not abort the pass —
                    # the co-claimed entries already paid a delivery count and
                    # deserve their retry; the unstamped row converges through
                    # the seen-TTL republish cycle.
                    try:
                        await on_dead(envelope)
                    except Exception:
                        logger.exception(
                            "queue.dead_letter_hook_failed",
                            stream=topic,
                            event_id=envelope.event_id,
                        )
                await self._to_dlq(topic, group, entry_id, envelope, times)
                continue
            deliveries.append(Delivery(handle=entry_id, envelope=envelope, delivery_count=times))
        return deliveries

    async def pending_depth(self, topic: str, group: str) -> int:
        summary = await self._redis.xpending(topic, group)
        return int(cast(Any, summary)["pending"]) if summary else 0

    async def oldest_pending_age_seconds(self, topic: str, group: str) -> float:
        try:
            summary = await self._redis.xpending(topic, group)
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                return 0.0  # group not created yet ⇒ nothing pending
            raise
        except IndexError:
            # An empty PEL: real Redis returns [0, nil, nil, nil], but fakeredis
            # returns [] and redis-py's parser trips on it — either way nothing
            # is pending, so the oldest-pending age is 0.
            return 0.0
        s = summary or {}
        if not s.get("pending"):
            return 0.0
        oldest_id = s.get("min")
        if not oldest_id:
            return 0.0
        # Redis stream ids are "<ms>-<seq>"; the ms is the entry's server-clock
        # enqueue time. Age against the Redis TIME (same clock, no host skew).
        entry_ms = int(str(oldest_id).split("-", 1)[0])
        clock = await self._redis.time()
        now_ms = int(clock[0]) * 1000 + int(clock[1]) // 1000
        return max(0.0, (now_ms - entry_ms) / 1000.0)

    async def stream_event_ids(self, topic: str, *, page: int = 1000) -> list[str]:
        """Every event_id currently present in the stream (read or unread, acked or
        not — XACK does not delete the entry). Redis-specific admin helper for the
        chaos ledger reconciliation: an event still in the stream is recoverable
        (re-readable/replayable), so it has NOT been lost even if unacked.

        Paginated (exclusive-min cursor) so a MAXLEN-sized stream (~100k) is never
        pulled into one call — bounded memory per page."""
        event_ids: list[str] = []
        cursor = "-"
        while True:
            entries = cast(
                "list[tuple[str, dict[str, str]]]",
                await self._redis.xrange(topic, min=cursor, max="+", count=page),
            )
            if not entries:
                break
            for _id, fields in entries:
                envelope = self._safe_parse(fields.get(_WIRE_FIELD))
                if envelope is not None:  # a corrupt frame is not a recoverable id
                    event_ids.append(envelope.event_id)
            if len(entries) < page:
                break
            cursor = f"({entries[-1][0]}"  # exclusive of the last id (Redis 6.2+)
        return event_ids

    async def pending_event_ids(self, topic: str, group: str) -> list[str]:
        """The event_ids of every message delivered but not yet acked for ``group``
        (the PEL). Redis-specific admin helper, not part of the QueueBus protocol —
        used by the chaos ledger reconciliation to distinguish in-flight work
        (will be redelivered) from genuinely dropped events. Reads each pending
        entry's envelope so the caller reconciles against durable event_ids, not
        broker-opaque stream entry ids. Tolerates a missing stream/group (returns
        ``[]``) — after a total queue wipe neither exists yet. If a pending entry
        was trimmed out of the stream (possible at MAXLEN scale), its id is omitted
        here — the reconciliation then treats it as not-pending, which over-reports
        (never hides) loss, the fail-safe direction for a zero-loss check."""
        if not await self._redis.exists(topic):
            return []
        try:
            pending = await self._redis.xpending_range(topic, group, min="-", max="+", count=1000)
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                return []
            raise
        event_ids: list[str] = []
        for item in cast("list[dict[str, Any]]", pending or []):
            entry_id = item["message_id"]
            entries = await self._redis.xrange(topic, min=entry_id, max=entry_id, count=1)
            for _id, fields in cast("list[tuple[str, dict[str, str]]]", entries):
                envelope = self._safe_parse(fields.get(_WIRE_FIELD))
                if envelope is not None:
                    event_ids.append(envelope.event_id)
        return event_ids

    async def forget(self, group: str, event_id: str) -> None:
        """Erase the seen-ledger entry for ``event_id`` (Redis-specific admin
        helper, not part of the QueueBus protocol). A dead-lettered event id is
        marked seen so republished copies are dropped; an operator REPLAY (e.g.
        clearing ingest_outbox.dead_at/published_at) must call this too, or the
        replayed publish is silently swallowed for the seen TTL."""
        await self._redis.delete(self._seen_key(group, event_id))

    # -- signature verification (TITAN-SEC-020) ----------------------------

    @staticmethod
    def _safe_parse(raw: str | None) -> Envelope | None:
        """Parse a wire frame, returning None on ANY malformation instead of
        raising — the reaper must never wedge on one corrupt entry."""
        if raw is None:
            return None
        try:
            return Envelope.from_wire(raw)
        except Exception:  # noqa: BLE001 — a bad frame is data, not a bug
            return None

    async def _verify_and_parse(
        self, topic: str, group: str, entry_id: str, fields: dict[str, str]
    ) -> Envelope | None:
        """Verify the SEC-020 signature and parse the envelope. On a missing/bad
        signature or unparseable frame, DROP it (ack so it never redelivers or
        wedges the reaper) and report it for the audit event — returning None so
        the caller skips it. With no signer configured, only parsing is guarded
        (backward-compatible with unsigned local/test buses)."""
        raw = fields.get(_WIRE_FIELD)
        if raw is None:
            await self._drop_invalid(topic, group, entry_id, reason="missing_data", envelope=None)
            return None
        if self._signer is not None:
            signature = fields.get(_WIRE_SIG)
            if not signature:
                await self._drop_invalid(
                    topic, group, entry_id, reason="missing_signature", envelope=None, raw=raw
                )
                return None
            if not self._signer.verify(raw, signature):
                # Best-effort claimed identity for forensics — the frame is
                # untrusted, so parse only to surface an event_id/actor.
                await self._drop_invalid(
                    topic,
                    group,
                    entry_id,
                    reason="bad_signature",
                    envelope=self._safe_parse(raw),
                    raw=raw,
                )
                return None
        envelope = self._safe_parse(raw)
        if envelope is None:
            await self._drop_invalid(
                topic, group, entry_id, reason="unparseable", envelope=None, raw=raw
            )
            return None
        # Bind the signature to THIS stream. The HMAC key is shared across
        # streams, so a signature valid on one stream is valid on another; without
        # this a Redis-write attacker could relay a legitimately-signed frame onto
        # a different stream and have it dispatched to the wrong handler. The
        # envelope already carries its own topic, so a mismatch is a relay.
        if self._signer is not None and envelope.topic != topic:
            await self._drop_invalid(
                topic, group, entry_id, reason="topic_mismatch", envelope=envelope, raw=raw
            )
            return None
        return envelope

    async def _drop_invalid(
        self,
        topic: str,
        group: str,
        entry_id: str,
        *,
        reason: str,
        envelope: Envelope | None,
        raw: str | None = None,
    ) -> None:
        """Drop a rejected envelope (TITAN-SEC-020 "consumers drop and audit").

        ACK removes it from the PEL so it neither redelivers nor wedges the
        reaper. Deliberately NOT dead-lettered and NOT marked-seen: dead-lettering
        would let a forged-frame flood grow the DLQ unbounded, and marking a
        forged (attacker-chosen) event_id seen would let an attacker poison the
        dedup ledger against a future legitimate event with that id."""
        await self._redis.xack(topic, group, entry_id)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else None
        event_id = envelope.event_id if envelope is not None else None
        actor = envelope.actor if envelope is not None else "unknown"
        logger.warning(
            "queue.envelope_rejected",
            stream=topic,
            reason=reason,
            event_id=event_id,
            claimed_actor=actor,
        )
        if self._on_invalid is not None:
            try:
                await self._on_invalid(
                    InvalidEnvelope(
                        topic=topic,
                        actor=actor,
                        reason=reason,
                        event_id=event_id,
                        payload_sha256=digest,
                    )
                )
            except Exception:
                logger.exception("queue.invalid_envelope_hook_failed", stream=topic)

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
        # A dead-lettered event_id is DISPOSED: mark it seen so a republished
        # copy (same stable id, e.g. an outbox stale-republish) is dropped at
        # consume instead of burning another full retry cycle into the DLQ.
        await self._mark_seen(group, envelope.event_id)
        depth = await self._redis.xlen(dlq)
        STREAM_DLQ_DEPTH.labels(stream=topic).set(depth)
        logger.warning(
            "queue.poison_message",
            stream=topic,
            event_id=envelope.event_id,
            delivery_count=times,
        )
