"""Signed Redis Streams envelopes (Phase 68, TITAN-SEC-020). Fakeredis, no infra.

Proves: publish signs; consume/reap verify; a tampered, forged (missing-sig), or
unparseable frame is DROPPED (never delivered) and reported to the audit hook —
and crucially does NOT wedge the reaper, the pre-existing failure mode an
unguarded parse before the poison check would re-introduce."""

import json
from typing import cast

import fakeredis.aioredis
from redis.asyncio import Redis

from tests.conftest import run_async
from titan.events.bus import Delivery, InvalidEnvelope
from titan.events.redis_bus import RedisStreamBus
from titan.vault.signing import HmacEnvelopeSigner

_TOPIC = "stream:events.domain"
_GROUP = "g"


def _bus_with_capture() -> tuple[RedisStreamBus, Redis, list[InvalidEnvelope]]:
    captured: list[InvalidEnvelope] = []

    async def on_invalid(rejection: InvalidEnvelope) -> None:
        captured.append(rejection)

    redis: Redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bus = RedisStreamBus(redis, signer=HmacEnvelopeSigner("k"), on_invalid=on_invalid)
    return bus, redis, captured


async def _first_entry(redis: Redis) -> tuple[str, dict[str, str]]:
    entries = cast("list[tuple[str, dict[str, str]]]", await redis.xrange(_TOPIC))
    return entries[0]


def test_signed_message_roundtrips() -> None:
    bus, _redis, captured = _bus_with_capture()

    async def go() -> list[Delivery]:
        await bus.ensure_group(_TOPIC, _GROUP)
        await bus.publish(_TOPIC, {"hello": "world"}, actor="api")
        return await bus.consume(_TOPIC, _GROUP, "c", block_ms=50)

    deliveries = run_async(go)
    assert len(deliveries) == 1
    assert deliveries[0].envelope.payload == {"hello": "world"}
    assert captured == []


def test_tampered_payload_is_dropped_and_audited() -> None:
    bus, redis, captured = _bus_with_capture()

    async def go() -> list[Delivery]:
        await bus.ensure_group(_TOPIC, _GROUP)
        await bus.publish(_TOPIC, {"amount": 1}, actor="api")
        # Attacker rewrites the payload but cannot re-sign it.
        _entry_id, fields = await _first_entry(redis)
        forged = json.loads(fields["data"])
        forged["payload"] = {"amount": 1000000}
        await redis.xadd(_TOPIC, {"data": json.dumps(forged), "sig": fields["sig"]})
        return await bus.consume(_TOPIC, _GROUP, "c", block_ms=50)

    deliveries = run_async(go)
    # Only the untampered original is delivered; the forged one is dropped.
    assert len(deliveries) == 1
    assert deliveries[0].envelope.payload == {"amount": 1}
    assert len(captured) == 1
    assert captured[0].reason == "bad_signature"
    assert captured[0].payload_sha256 is not None


def test_missing_signature_is_dropped() -> None:
    bus, redis, captured = _bus_with_capture()

    async def go() -> list[Delivery]:
        await bus.ensure_group(_TOPIC, _GROUP)
        # An attacker who can XADD but cannot sign injects an unsigned frame.
        unsigned = {
            "event_id": "e1",
            "topic": _TOPIC,
            "occurred_at": "2026-07-21T00:00:00Z",
            "actor": "attacker",
            "payload": {},
        }
        await redis.xadd(_TOPIC, {"data": json.dumps(unsigned)})
        return await bus.consume(_TOPIC, _GROUP, "c", block_ms=50)

    deliveries = run_async(go)
    assert deliveries == []
    assert captured[0].reason == "missing_signature"


def test_unparseable_frame_is_dropped_not_raised() -> None:
    bus, redis, captured = _bus_with_capture()
    signer = HmacEnvelopeSigner("k")

    async def go() -> list[Delivery]:
        await bus.ensure_group(_TOPIC, _GROUP)
        garbage = "not json at all"
        # Correctly signed but structurally garbage: verification passes, parse fails.
        await redis.xadd(_TOPIC, {"data": garbage, "sig": signer.sign(garbage)})
        return await bus.consume(_TOPIC, _GROUP, "c", block_ms=50)

    deliveries = run_async(go)
    assert deliveries == []
    assert captured[0].reason == "unparseable"


def test_bad_frame_does_not_wedge_the_reaper() -> None:
    # The regression guard: an unguarded from_wire() before the poison check made
    # the reaper raise on every pass forever. A forged frame must be dropped, and
    # a genuine pending message must still be reclaimed.
    bus, redis, _captured = _bus_with_capture()

    async def go() -> tuple[list[Delivery], list[Delivery]]:
        await bus.ensure_group(_TOPIC, _GROUP)
        # A forged (unsigned) frame that a naive reaper would choke on.
        await redis.xadd(_TOPIC, {"data": "{bad", "sig": "x"})
        # Deliver a REAL message and leave it pending (consume, no ack).
        await bus.publish(_TOPIC, {"real": True}, actor="api")
        delivered = await bus.consume(_TOPIC, _GROUP, "c1", block_ms=50)
        # Reaper with min_idle_ms=0 reclaims the still-pending real message and
        # must not raise on the forged frame sitting in the stream.
        reclaimed = await bus.reap(_TOPIC, _GROUP, "c2", min_idle_ms=0)
        return delivered, reclaimed

    delivered, reclaimed = run_async(go)
    # The forged frame never became a delivery; the real one did and was reclaimed.
    assert [d.envelope.payload for d in delivered] == [{"real": True}]
    assert [d.envelope.payload for d in reclaimed] == [{"real": True}]


def test_signed_frame_relayed_to_another_stream_is_rejected() -> None:
    # FIX E: the HMAC key is shared across streams, so a signature valid on one
    # stream is valid on another. Binding to the read stream stops a Redis-write
    # attacker relaying a legit frame onto a different stream's handler.
    bus, redis, captured = _bus_with_capture()
    other = "stream:runs.execute"

    async def go() -> list[Delivery]:
        await bus.ensure_group(_TOPIC, _GROUP)
        await bus.ensure_group(other, _GROUP)
        await bus.publish(_TOPIC, {"legit": True}, actor="api")
        # Attacker copies the validly-signed (data, sig) onto a DIFFERENT stream.
        _entry_id, fields = await _first_entry(redis)
        await redis.xadd(other, {"data": fields["data"], "sig": fields["sig"]})
        return await bus.consume(other, _GROUP, "c", block_ms=50)

    deliveries = run_async(go)
    assert deliveries == []  # relayed frame dropped despite a valid signature
    assert captured[0].reason == "topic_mismatch"


def test_unsigned_bus_stays_backward_compatible() -> None:
    # With no signer (existing local/test buses), publish is unsigned and consume
    # does not verify — nothing regresses.
    redis: Redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bus = RedisStreamBus(redis)

    async def go() -> list[Delivery]:
        await bus.ensure_group(_TOPIC, _GROUP)
        await bus.publish(_TOPIC, {"x": 1}, actor="api")
        return await bus.consume(_TOPIC, _GROUP, "c", block_ms=50)

    assert len(run_async(go)) == 1
