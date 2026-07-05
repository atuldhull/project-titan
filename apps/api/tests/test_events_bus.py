"""QueueBus mechanics (fakeredis, no infra): dedup, reclaim, poison→DLQ.

fakeredis implements Redis Streams (xreadgroup/xautoclaim/xpending), so these
run in the fast non-DB suite.
"""

import fakeredis.aioredis

from tests.conftest import run_async
from titan.events.redis_bus import RedisStreamBus
from titan.events.streams import STREAM_EVENTS_DOMAIN, dlq_stream

GROUP = "test-group"
TOPIC = STREAM_EVENTS_DOMAIN


def _bus() -> tuple[RedisStreamBus, fakeredis.aioredis.FakeRedis]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisStreamBus(redis), redis


def test_publish_consume_ack_round_trip() -> None:
    async def scenario() -> None:
        bus, _ = _bus()
        await bus.ensure_group(TOPIC, GROUP)
        eid = await bus.publish(TOPIC, {"n": 1}, actor="api", trace_id="t1")

        got = await bus.consume(TOPIC, GROUP, "c1", block_ms=100)
        assert len(got) == 1
        assert got[0].envelope.event_id == eid
        assert got[0].envelope.payload == {"n": 1}
        assert got[0].envelope.actor == "api" and got[0].envelope.trace_id == "t1"

        await bus.ack(TOPIC, GROUP, got[0])
        # Nothing left to read.
        assert await bus.consume(TOPIC, GROUP, "c1", block_ms=100) == []

    run_async(scenario)


def test_provided_event_id_is_preserved() -> None:
    async def scenario() -> None:
        bus, _ = _bus()
        await bus.ensure_group(TOPIC, GROUP)
        eid = await bus.publish(TOPIC, {}, actor="sys", event_id="fixed-1")
        assert eid == "fixed-1"

    run_async(scenario)


def test_duplicate_delivery_is_a_no_op() -> None:
    """Re-publishing the SAME event_id after it was acked yields no delivery
    (dedup on seen:{group}) — TITAN-BCK-010 'duplicate delivery is a no-op'."""

    async def scenario() -> None:
        bus, _ = _bus()
        await bus.ensure_group(TOPIC, GROUP)
        await bus.publish(TOPIC, {"n": 1}, actor="api", event_id="dup-1")
        first = await bus.consume(TOPIC, GROUP, "c1", block_ms=100)
        assert len(first) == 1
        await bus.ack(TOPIC, GROUP, first[0])

        # Same event_id lands again (e.g. an outbox re-drain).
        await bus.publish(TOPIC, {"n": 1}, actor="api", event_id="dup-1")
        second = await bus.consume(TOPIC, GROUP, "c1", block_ms=100)
        assert second == []  # deduped: no second delivery, no side effect

    run_async(scenario)


def test_killed_consumer_pending_is_reclaimed() -> None:
    """A message left pending by a dead consumer is reclaimed by another
    (XAUTOCLAIM). min_idle=0 forces immediate reclaim in the test; production
    uses 60 s (DoD: reclaim in <60 s)."""

    async def scenario() -> None:
        bus, _ = _bus()
        await bus.ensure_group(TOPIC, GROUP)
        await bus.publish(TOPIC, {"n": 1}, actor="api", event_id="orphan-1")

        # c1 reads but "dies" without acking (nack leaves it pending).
        got = await bus.consume(TOPIC, GROUP, "c1", block_ms=100)
        assert len(got) == 1
        await bus.nack(TOPIC, GROUP, got[0])

        # c2 reaps and reclaims the orphaned message.
        reclaimed = await bus.reap(TOPIC, GROUP, "c2", min_idle_ms=0)
        assert len(reclaimed) == 1
        assert reclaimed[0].envelope.event_id == "orphan-1"
        assert reclaimed[0].delivery_count >= 2  # redelivered

    run_async(scenario)


def test_pending_depth_reports_unacked() -> None:
    async def scenario() -> None:
        bus, _ = _bus()
        await bus.ensure_group(TOPIC, GROUP)
        await bus.publish(TOPIC, {}, actor="api", event_id="p1")
        await bus.publish(TOPIC, {}, actor="api", event_id="p2")
        assert await bus.pending_depth(TOPIC, GROUP) == 0  # nothing delivered yet
        got = await bus.consume(TOPIC, GROUP, "c1", block_ms=100)
        assert await bus.pending_depth(TOPIC, GROUP) == 2  # both delivered, unacked
        await bus.ack(TOPIC, GROUP, got[0])
        assert await bus.pending_depth(TOPIC, GROUP) == 1

    run_async(scenario)


def test_poison_message_goes_to_dlq_after_three_attempts() -> None:
    """A message that is never acked is dead-lettered once its delivery count
    exceeds max_attempts (DoD: 'poison lands in the DLQ after exactly 3
    attempts'). Attempts: 1 initial read + reaps 2,3; the reap that would make
    it a 4th delivery routes it to the DLQ instead."""

    async def scenario() -> None:
        bus, redis = _bus()
        await bus.ensure_group(TOPIC, GROUP)
        await bus.publish(TOPIC, {"bad": True}, actor="api", event_id="poison-1")

        # attempt 1 (initial delivery), left pending
        d1 = await bus.consume(TOPIC, GROUP, "c1", block_ms=100)
        assert len(d1) == 1
        await bus.nack(TOPIC, GROUP, d1[0])

        # attempt 2, still pending
        d2 = await bus.reap(TOPIC, GROUP, "c2", min_idle_ms=0, max_attempts=3)
        assert len(d2) == 1 and d2[0].delivery_count == 2
        await bus.nack(TOPIC, GROUP, d2[0])

        # attempt 3, still pending
        d3 = await bus.reap(TOPIC, GROUP, "c3", min_idle_ms=0, max_attempts=3)
        assert len(d3) == 1 and d3[0].delivery_count == 3
        await bus.nack(TOPIC, GROUP, d3[0])

        # 4th reclaim → over max_attempts → DLQ, not returned
        d4 = await bus.reap(TOPIC, GROUP, "c4", min_idle_ms=0, max_attempts=3)
        assert d4 == []
        assert await redis.xlen(dlq_stream(TOPIC)) == 1
        # Original stream has no pending entries left.
        assert await redis.xack(TOPIC, GROUP, "0-0") == 0  # nothing to ack

    run_async(scenario)
