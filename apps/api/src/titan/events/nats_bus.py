"""NATS JetStream implementation of QueueBus (ADR-005 / ADR-BCK-02, TITAN-FND-051).

The documented scale-out path past ~5,000 tasks/min (§9): a config-only swap
(QUEUE_BACKEND=nats) behind the SAME QueueBus Protocol and the SAME Envelope, so
no domain code changes. The Redis Streams reference lives in ``redis_bus.py``; this
maps its semantics onto JetStream primitives:

  Redis Streams                         JetStream
  ------------------------------------  ------------------------------------------
  XADD (MAXLEN ~N)                      js.publish(subject) on a max_msgs stream
  consumer group + XREADGROUP '>'       durable PULL consumer + fetch()
  XPENDING times_delivered              msg.metadata.num_delivered
  XACK                                  msg.ack()
  leave pending (nack) + XAUTOCLAIM     msg.nak() (immediate redeliver) + fetch()
  seen:{group}:{id} keys (24h TTL)      a KV bucket per group (bucket max_age=24h)
  DLQ stream after max_attempts         reap() publishes to the .dlq stream + term()

Both consume() and reap() fetch from the one shared durable (JetStream has no
"claim from a dead consumer" — an unacked msg is redelivered to whoever fetches
next once nak'd/ack_wait-expired); they differ only in intent: consume() takes new
work, reap() sweeps redelivered work and routes poison (num_delivered > max_attempts)
to the DLQ. Dedup is at CONSUME (seen ledger set on ack), matching the Redis model,
NOT JetStream publish-dedup (whose window is too short for the 24h "seen" contract).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
from typing import TYPE_CHECKING, Any, cast

import structlog
import uuid_utils

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

if TYPE_CHECKING:
    from nats.aio.msg import Msg
    from nats.js import JetStreamContext

logger = structlog.get_logger("titan.events")

_SIG_HEADER = "Titan-Sig"  # TITAN-SEC-020 HMAC over the message body
SEEN_TTL_SECONDS = 24 * 60 * 60
_SUBJECT_PREFIX = "titan.q"
_REAP_FETCH_MS = 500  # window to collect nak'd/redelivered msgs (redelivery is near-immediate)


def _sanitize(topic: str) -> str:
    """A NATS-safe token for a Redis-style topic. JetStream stream names forbid
    ``. * > /`` and whitespace, and subject tokens split on ``.`` — so collapse
    every non [A-Za-z0-9_] char to ``_`` (deterministic, collision-free across the
    fixed ALL_STREAMS set + their .dlq siblings)."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in topic)


def subject_for(topic: str) -> str:
    return f"{_SUBJECT_PREFIX}.{_sanitize(topic)}"


def stream_name_for(topic: str) -> str:
    return f"TITAN_Q_{_sanitize(topic)}"


class NatsStreamBus:
    """QueueBus over NATS JetStream. One instance per process; ``connect()`` must
    be awaited before use (the factory does this). Holds in-flight msgs between
    consume/reap and ack/nack so a str ``handle`` can round-trip to msg.ack()."""

    def __init__(
        self,
        js: JetStreamContext,
        *,
        nc: Any | None = None,
        max_msgs: int = 100_000,
        ack_wait_seconds: float = 60.0,
        signer: Signer | None = None,
        on_invalid: InvalidEnvelopeHook | None = None,
    ) -> None:
        self._js = js
        self._nc = nc  # owned connection (set by the factory) so aclose() can close it
        self._max_msgs = max_msgs
        self._ack_wait = ack_wait_seconds
        self._signer = signer
        self._on_invalid = on_invalid
        self._subs: dict[tuple[str, str], Any] = {}  # (stream, group) -> pull subscription
        self._inflight: dict[str, Msg] = {}  # handle -> msg (for ack/nack/term)
        self._kv: dict[str, Any] = {}  # group -> KV store (seen ledger)

    async def aclose(self) -> None:
        """Close the owned NATS connection (no-op when js was injected, e.g. in tests)."""
        if self._nc is not None:
            await self._nc.close()

    async def ensure_group(self, topic: str, group: str) -> None:
        """Idempotently create the stream, the DLQ stream, the durable pull consumer,
        and the seen-ledger KV bucket for ``group``."""
        from nats.js.api import (
            AckPolicy,
            ConsumerConfig,
            KeyValueConfig,
            RetentionPolicy,
            StorageType,
            StreamConfig,
        )
        from nats.js.errors import BadRequestError, NotFoundError

        for tpc in (topic, dlq_stream(topic)):
            cfg = StreamConfig(
                name=stream_name_for(tpc),
                subjects=[subject_for(tpc)],
                max_msgs=self._max_msgs,
                retention=RetentionPolicy.LIMITS,
                storage=StorageType.FILE,
            )
            try:
                await self._js.add_stream(cfg)
            except BadRequestError:
                await self._js.update_stream(cfg)  # already exists → reconcile config

        consumer = ConsumerConfig(
            durable_name=group,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self._ack_wait,
            max_deliver=-1,  # unlimited; reap() owns the DLQ threshold, not JetStream
        )
        with contextlib.suppress(BadRequestError):  # durable already exists
            await self._js.add_consumer(stream_name_for(topic), consumer)

        bucket = self._seen_bucket(group)
        try:
            self._kv[group] = await self._js.key_value(bucket)
        except NotFoundError:
            # nats-py wants ttl as float SECONDS (compared against a 120s dedup window),
            # not a timedelta; the bucket max_age gives the 24h "seen" TTL.
            self._kv[group] = await self._js.create_key_value(
                KeyValueConfig(bucket=bucket, ttl=float(SEEN_TTL_SECONDS))
            )

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
        wire = envelope.to_wire().encode("utf-8")
        headers = {_SIG_HEADER: self._signer.sign(envelope.to_wire())} if self._signer else None
        await self._js.publish(subject_for(topic), wire, headers=headers)
        return envelope.event_id

    async def consume(
        self, topic: str, group: str, consumer: str, *, count: int = 10, block_ms: int = 5000
    ) -> list[Delivery]:
        msgs = await self._fetch(topic, group, count, block_ms)
        deliveries: list[Delivery] = []
        for msg in msgs:
            envelope = await self._verify_and_parse(topic, group, msg)
            if envelope is None:
                continue
            if await self._already_seen(group, envelope.event_id):
                await msg.ack()  # duplicate: ack + drop, no side effect (TITAN-BCK-010)
                continue
            handle = self._track(msg)
            deliveries.append(
                Delivery(
                    handle=handle, envelope=envelope, delivery_count=msg.metadata.num_delivered
                )
            )
        return deliveries

    async def ack(self, topic: str, group: str, delivery: Delivery) -> None:
        msg = self._inflight.pop(delivery.handle, None)
        if msg is not None:
            await msg.ack()
        await self._mark_seen(group, delivery.envelope.event_id)

    async def nack(self, topic: str, group: str, delivery: Delivery) -> None:
        # Immediate redelivery on the next fetch (matches Redis "leave pending" +
        # the reaper reclaiming it; min_idle 0 in tests). Do NOT mark seen.
        msg = self._inflight.pop(delivery.handle, None)
        if msg is not None:
            await msg.nak()

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
        # Redis reclaims only entries idle > min_idle; JetStream redelivers a nak'd or
        # ack_wait-expired msg to any fetch, so a fetch with a real window collects the
        # redelivered set. (min_idle_ms maps to JetStream's ack_wait, set at ensure time;
        # a nak() is immediate, so the reap window need only cover redelivery latency.)
        msgs = await self._fetch(topic, group, 100, _REAP_FETCH_MS)
        deliveries: list[Delivery] = []
        for msg in msgs:
            envelope = await self._verify_and_parse(topic, group, msg)
            if envelope is None:
                continue  # dropped + audited — never counts against max_attempts
            if await self._already_seen(group, envelope.event_id):
                await msg.ack()
                continue
            times = msg.metadata.num_delivered
            if times > max_attempts:
                if on_dead is not None:
                    # Let the producer's canonical store record the quarantine BEFORE the
                    # DLQ write (a crash between leaves the msg redeliverable → re-detected).
                    # A failing hook must not abort the pass — co-fetched msgs deserve retry.
                    try:
                        await on_dead(envelope)
                    except Exception:
                        logger.exception(
                            "queue.dead_letter_hook_failed",
                            stream=topic,
                            event_id=envelope.event_id,
                        )
                await self._to_dlq(topic, group, msg, envelope, times)
                continue
            handle = self._track(msg)
            deliveries.append(Delivery(handle=handle, envelope=envelope, delivery_count=times))
        return deliveries

    async def pending_depth(self, topic: str, group: str) -> int:
        try:
            info = await self._js.consumer_info(stream_name_for(topic), group)
        except Exception:
            return 0
        return int(info.num_ack_pending or 0)

    async def oldest_pending_age_seconds(self, topic: str, group: str) -> float:
        """Best-effort: the age of the oldest un-acked message = the message just past
        the consumer's contiguous ack floor. Not conformance-tested (a §9 backpressure
        metric); returns 0.0 when nothing is pending or the lookup fails."""
        try:
            info = await self._js.consumer_info(stream_name_for(topic), group)
            if not info.num_ack_pending:
                return 0.0
            floor = info.ack_floor.stream_seq if info.ack_floor else 0
            msg = await self._js.get_msg(stream_name_for(topic), seq=floor + 1)
        except Exception:
            return 0.0
        if msg is None or msg.time is None:
            return 0.0
        ts = msg.time if isinstance(msg.time, dt.datetime) else dt.datetime.fromisoformat(msg.time)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.UTC)
        return max(0.0, (dt.datetime.now(dt.UTC) - ts).total_seconds())

    # -- internals ---------------------------------------------------------

    async def _fetch(self, topic: str, group: str, count: int, timeout_ms: int) -> list[Msg]:
        from nats.errors import TimeoutError as NatsTimeout

        sub = await self._pull_sub(topic, group)
        try:
            return cast("list[Msg]", await sub.fetch(count, timeout=max(timeout_ms, 1) / 1000.0))
        except NatsTimeout:
            return []  # no messages available within the window

    async def _pull_sub(self, topic: str, group: str) -> Any:
        key = (stream_name_for(topic), group)
        sub = self._subs.get(key)
        if sub is None:
            sub = await self._js.pull_subscribe(
                subject_for(topic), durable=group, stream=stream_name_for(topic)
            )
            self._subs[key] = sub
        return sub

    def _track(self, msg: Msg) -> str:
        handle = f"{msg.metadata.stream}:{msg.metadata.sequence.stream}"
        self._inflight[handle] = msg
        return handle

    def _seen_bucket(self, group: str) -> str:
        return f"titan_seen_{_sanitize(group)}"

    def _seen_key(self, event_id: str) -> str:
        return _sanitize(event_id)

    async def _already_seen(self, group: str, event_id: str) -> bool:
        from nats.js.errors import KeyNotFoundError

        kv = self._kv.get(group)
        if kv is None:
            return False
        try:
            await kv.get(self._seen_key(event_id))
            return True
        except KeyNotFoundError:
            return False

    async def _mark_seen(self, group: str, event_id: str) -> None:
        kv = self._kv.get(group)
        if kv is not None:
            await kv.put(self._seen_key(event_id), b"1")

    async def _to_dlq(
        self, topic: str, group: str, msg: Msg, envelope: Envelope, times: int
    ) -> None:
        await self._js.publish(
            subject_for(dlq_stream(topic)),
            envelope.to_wire().encode("utf-8"),
            headers={
                "Titan-Origin-Stream": topic,
                "Titan-Delivery-Count": str(times),
            },
        )
        await msg.term()  # terminate: JetStream stops redelivering this message
        await self._mark_seen(group, envelope.event_id)  # disposed: drop republished copies
        try:
            info = await self._js.stream_info(stream_name_for(dlq_stream(topic)))
            STREAM_DLQ_DEPTH.labels(stream=topic).set(info.state.messages)
        except Exception:
            pass
        logger.warning(
            "queue.poison_message", stream=topic, event_id=envelope.event_id, delivery_count=times
        )

    # -- signature verification (TITAN-SEC-020) ----------------------------

    @staticmethod
    def _safe_parse(raw: str | None) -> Envelope | None:
        if raw is None:
            return None
        try:
            return Envelope.from_wire(raw)
        except Exception:  # noqa: BLE001 — a bad frame is data, not a bug
            return None

    async def _verify_and_parse(self, topic: str, group: str, msg: Msg) -> Envelope | None:
        """Verify the SEC-020 signature and parse; on a missing/bad signature or
        unparseable frame, DROP (ack so it never redelivers) + audit, and return None."""
        raw = msg.data.decode("utf-8") if msg.data else None
        if raw is None:
            await self._drop_invalid(topic, group, msg, reason="missing_data", envelope=None)
            return None
        if self._signer is not None:
            signature = (msg.headers or {}).get(_SIG_HEADER)
            if not signature:
                await self._drop_invalid(
                    topic, group, msg, reason="missing_signature", envelope=None, raw=raw
                )
                return None
            if not self._signer.verify(raw, signature):
                await self._drop_invalid(
                    topic,
                    group,
                    msg,
                    reason="bad_signature",
                    envelope=self._safe_parse(raw),
                    raw=raw,
                )
                return None
        envelope = self._safe_parse(raw)
        if envelope is None:
            await self._drop_invalid(
                topic, group, msg, reason="unparseable", envelope=None, raw=raw
            )
            return None
        # Bind the signature to THIS stream: a shared HMAC key means a frame signed for
        # one stream verifies on another; the envelope's own topic must match (SEC-020).
        if self._signer is not None and envelope.topic != topic:
            await self._drop_invalid(
                topic, group, msg, reason="topic_mismatch", envelope=envelope, raw=raw
            )
            return None
        return envelope

    async def _drop_invalid(
        self,
        topic: str,
        group: str,
        msg: Msg,
        *,
        reason: str,
        envelope: Envelope | None,
        raw: str | None = None,
    ) -> None:
        """ACK a rejected frame so it neither redelivers nor wedges the reaper.
        Deliberately NOT dead-lettered and NOT marked-seen (a forged flood would grow
        the DLQ; a forged event_id would poison the dedup ledger) — TITAN-SEC-020."""
        await msg.ack()
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
