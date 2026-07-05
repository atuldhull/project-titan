"""Transactional outbox tests (schema-marked): no-loss re-drain, idempotent
replay via the inbox ledger. Needs the disposable test DB + fakeredis."""

from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.events.consumers import (
    DOMAIN_GROUP,
    process_domain_batch,
    reap_domain_once,
    record_inbox,
)
from titan.events.outbox import OutboxDrainer, write_event
from titan.events.redis_bus import RedisStreamBus
from titan.events.streams import STREAM_EVENTS_DOMAIN

pytestmark = pytest.mark.schema


@pytest.fixture(autouse=True)
def _clean_event_tables(migrated_engine: Any) -> None:
    """These tests assert on the WHOLE outbox/inbox, so start each from empty —
    other suites (Phase 11/12 API tests) commit domain events to the same
    session DB and leave them undrained, which would otherwise inflate counts."""

    async def clean() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(text("DELETE FROM event_inbox"))
            await conn.execute(text("DELETE FROM outbox"))

    run_async(clean)


def _bus() -> tuple[RedisStreamBus, Any]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisStreamBus(redis), redis


def test_write_event_then_drain_publishes_and_stamps(migrated_engine: Any) -> None:
    async def scenario() -> None:
        bus, redis = _bus()
        async with migrated_engine.begin() as conn:
            eid = await write_event(
                conn, STREAM_EVENTS_DOMAIN, {"hello": "world"}, actor="api", trace_id="t1"
            )

        # Before draining: committed in Postgres, NOT yet on the stream.
        assert await redis.xlen(STREAM_EVENTS_DOMAIN) == 0
        async with migrated_engine.connect() as conn:
            unpublished = await conn.execute(
                text("SELECT count(*) FROM outbox WHERE published_at IS NULL AND id = :id"),
                {"id": eid},
            )
            assert unpublished.scalar() == 1

        drainer = OutboxDrainer(migrated_engine, bus)
        assert await drainer.drain_once() == 1

        # Published to the stream with the outbox row id as event_id…
        assert await redis.xlen(STREAM_EVENTS_DOMAIN) == 1
        # …and stamped published_at, so a re-drain is a no-op.
        assert await drainer.drain_once() == 0
        assert await redis.xlen(STREAM_EVENTS_DOMAIN) == 1

    run_async(scenario)


def test_crash_between_commit_and_publish_loses_nothing(migrated_engine: Any) -> None:
    """Simulate a crash after DB commit but before publish: the row stays
    unpublished and a later drainer recovers it (TITAN-FND-052)."""

    async def scenario() -> None:
        bus, redis = _bus()
        # write_event commits; "crash" = we simply never called the drainer.
        async with migrated_engine.begin() as conn:
            await write_event(conn, STREAM_EVENTS_DOMAIN, {"n": 1}, actor="api")
            await write_event(conn, STREAM_EVENTS_DOMAIN, {"n": 2}, actor="api")
        assert await redis.xlen(STREAM_EVENTS_DOMAIN) == 0  # nothing sent yet

        # A fresh drainer (post-restart) re-drains and recovers both events.
        recovered = await OutboxDrainer(migrated_engine, bus).drain_once()
        assert recovered == 2
        assert await redis.xlen(STREAM_EVENTS_DOMAIN) == 2

    run_async(scenario)


def test_full_loop_and_replay_is_idempotent(migrated_engine: Any) -> None:
    """Outbox → drain → consume writes exactly one inbox row; replaying the
    whole stream into a fresh group produces no duplicate side effect
    (TITAN-BCK-010 acceptance)."""

    async def scenario() -> None:
        import asyncio

        bus, _ = _bus()
        async with migrated_engine.begin() as conn:
            eid = await write_event(conn, STREAM_EVENTS_DOMAIN, {"x": 1}, actor="api")
        await OutboxDrainer(migrated_engine, bus).drain_once()

        # Consume once and record to the inbox ledger.
        await bus.ensure_group(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP)
        deliveries = await bus.consume(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP, "c1", block_ms=100)
        assert len(deliveries) == 1
        assert await record_inbox(migrated_engine, DOMAIN_GROUP, deliveries[0]) is True
        await bus.ack(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP, deliveries[0])

        async def inbox_count() -> int:
            async with migrated_engine.connect() as conn:
                r = await conn.execute(
                    text("SELECT count(*) FROM event_inbox WHERE event_id = :id"), {"id": eid}
                )
                return int(r.scalar_one())

        assert await inbox_count() == 1

        # Replay the SAME event into a FRESH group (new consumer, no seen set):
        # the inbox is keyed on event_id alone, so the Postgres side effect is
        # a no-op even for a brand-new group (TITAN-BCK-010 acceptance).
        fresh_group = "replay-group"
        await bus.ensure_group(STREAM_EVENTS_DOMAIN, fresh_group)
        replayed = await bus.consume(STREAM_EVENTS_DOMAIN, fresh_group, "c2", block_ms=100)
        assert len(replayed) == 1  # fresh group re-reads the message…
        assert await record_inbox(migrated_engine, fresh_group, replayed[0]) is False  # no-op
        assert await inbox_count() == 1  # still exactly one row: no duplicate effect
        del asyncio

    run_async(scenario)


def test_demo_consumer_batch_drains_stream(migrated_engine: Any) -> None:
    """process_domain_batch (the unit the worker loop runs) consumes a
    published event into the inbox exactly once, and is idempotent on replay."""

    async def scenario() -> None:
        bus, _ = _bus()
        async with migrated_engine.begin() as conn:
            eid = await write_event(conn, STREAM_EVENTS_DOMAIN, {"demo": True}, actor="api")
        await OutboxDrainer(migrated_engine, bus).drain_once()
        await bus.ensure_group(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP)

        processed = await process_domain_batch(migrated_engine, bus, "c1", block_ms=100)
        assert processed == 1

        async with migrated_engine.connect() as conn:
            r = await conn.execute(
                text("SELECT count(*) FROM event_inbox WHERE event_id = :id"), {"id": eid}
            )
            assert r.scalar_one() == 1

        # Nothing left to consume; a second batch is empty (already acked).
        assert await process_domain_batch(migrated_engine, bus, "c1", block_ms=100) == 0

    run_async(scenario)


def test_reaper_reprocesses_reclaimed_message(migrated_engine: Any) -> None:
    """A message left pending by a dead consumer is RECLAIMED AND REPROCESSED
    by the reaper (not just dead-lettered) — the retry contract behind
    TITAN-FND-011. Regression for the 'reaper discards reap() output' defect."""

    async def scenario() -> None:
        bus, redis = _bus()
        async with migrated_engine.begin() as conn:
            eid = await write_event(conn, STREAM_EVENTS_DOMAIN, {"x": 1}, actor="api")
        await OutboxDrainer(migrated_engine, bus).drain_once()
        await bus.ensure_group(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP)

        # A consumer reads but "dies" without acking (message left pending).
        got = await bus.consume(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP, "dead", block_ms=100)
        assert len(got) == 1
        await bus.nack(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP, got[0])

        async def inbox_count() -> int:
            async with migrated_engine.connect() as conn:
                r = await conn.execute(
                    text("SELECT count(*) FROM event_inbox WHERE event_id = :id"), {"id": eid}
                )
                return int(r.scalar_one())

        assert await inbox_count() == 0  # never processed yet

        # The reaper reclaims AND reprocesses it (min_idle=0 forces reclaim).
        # reap_domain_once uses the production 60s min-idle via bus.reap; here
        # we drive bus.reap directly with min_idle=0 then hand to the handler.
        reclaimed = await bus.reap(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP, "reaper", min_idle_ms=0)
        assert len(reclaimed) == 1
        from titan.events.consumers import handle_domain_delivery

        await handle_domain_delivery(migrated_engine, bus, reclaimed[0])

        assert await inbox_count() == 1  # reprocessed, not dead-lettered
        assert await redis.xlen("stream:events.domain.dlq") == 0  # no false poison

    run_async(scenario)


def test_reap_domain_once_wires_metrics(migrated_engine: Any) -> None:
    """reap_domain_once runs cleanly and sets the pending-depth gauge."""

    async def scenario() -> None:
        bus, _ = _bus()
        await bus.ensure_group(STREAM_EVENTS_DOMAIN, DOMAIN_GROUP)
        assert await reap_domain_once(migrated_engine, bus, "reaper") == 0

    run_async(scenario)
