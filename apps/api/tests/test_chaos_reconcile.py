"""Ledger reconciliation (TITAN-FND-052 DoD item 2). Schema-marked (test DB) +
fakeredis for real Redis-Streams semantics. Proves the reconciliation both gives
a clean bill when every committed event is acknowledged AND detects genuine
acknowledged loss — while never mistaking recoverable in-flight/unread work for
loss."""

from typing import Any

import fakeredis.aioredis
import pytest

from tests.conftest import run_async
from titan.chaos.harness import ChaosHarness
from titan.chaos.reconcile import domain_dlq_stream, reconcile_domain_ledger
from titan.events.consumers import DOMAIN_GROUP
from titan.events.redis_bus import RedisStreamBus
from titan.events.streams import STREAM_EVENTS_DOMAIN

pytestmark = pytest.mark.schema

_STREAM = STREAM_EVENTS_DOMAIN


def _mk(engine: Any) -> tuple[ChaosHarness, RedisStreamBus, Any]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bus = RedisStreamBus(redis)
    return ChaosHarness(engine, bus, redis), bus, redis


def test_reconcile_clean_when_every_event_acked(migrated_engine: Any) -> None:
    async def scenario() -> None:
        harness, bus, _ = _mk(migrated_engine)
        seed = await harness.seed(events=3)
        await bus.ensure_group(_STREAM, DOMAIN_GROUP)
        await harness._publish_events(seed.event_ids)
        await harness._consume_and_ack("c")
        report = await reconcile_domain_ledger(migrated_engine, bus, event_ids=set(seed.event_ids))
        assert report.durable == 3 and report.acknowledged == 3
        assert report.dropped == [] and report.orphaned == []
        assert report.reconciled and report.acknowledged_task_loss == 0

    run_async(scenario)


def test_reconcile_detects_genuine_loss(migrated_engine: Any) -> None:
    async def scenario() -> None:
        harness, bus, redis = _mk(migrated_engine)
        seed = await harness.seed(events=3)
        await bus.ensure_group(_STREAM, DOMAIN_GROUP)
        await harness._publish_events(seed.event_ids)  # durable + published, never consumed
        await redis.flushall()  # total queue loss with no ack and no replay
        report = await reconcile_domain_ledger(migrated_engine, bus, event_ids=set(seed.event_ids))
        assert report.durable == 3 and report.acknowledged == 0
        assert sorted(report.dropped) == sorted(seed.event_ids)
        assert report.acknowledged_task_loss == 3 and not report.reconciled

    run_async(scenario)


def test_pending_inflight_is_not_counted_as_loss(migrated_engine: Any) -> None:
    async def scenario() -> None:
        harness, bus, _ = _mk(migrated_engine)
        seed = await harness.seed(events=3)
        await bus.ensure_group(_STREAM, DOMAIN_GROUP)
        await harness._publish_events(seed.event_ids)
        # Delivered to a consumer but unacked → pending, awaiting redelivery.
        await bus.consume(_STREAM, DOMAIN_GROUP, "worker", count=10, block_ms=10)
        report = await reconcile_domain_ledger(migrated_engine, bus, event_ids=set(seed.event_ids))
        assert report.pending == 3 and report.dropped == [] and report.reconciled

    run_async(scenario)


def test_unread_in_stream_is_not_counted_as_loss(migrated_engine: Any) -> None:
    async def scenario() -> None:
        harness, bus, _ = _mk(migrated_engine)
        seed = await harness.seed(events=3)
        await bus.ensure_group(_STREAM, DOMAIN_GROUP)
        await harness._publish_events(
            seed.event_ids
        )  # in the stream, never delivered — still recoverable
        report = await reconcile_domain_ledger(migrated_engine, bus, event_ids=set(seed.event_ids))
        assert report.dropped == [] and report.reconciled

    run_async(scenario)


def test_global_mode_accounts_for_this_runs_events(migrated_engine: Any) -> None:
    """The operator/global reconciliation (no ``event_ids``) scans the whole domain
    ledger; a run whose events were all acknowledged never appears in ``dropped``."""

    async def scenario() -> None:
        harness, bus, _ = _mk(migrated_engine)
        seed = await harness.seed(events=3)
        await bus.ensure_group(_STREAM, DOMAIN_GROUP)
        await harness._publish_events(seed.event_ids)
        await harness._consume_and_ack("c")
        report = await reconcile_domain_ledger(migrated_engine, bus)  # global scan
        assert report.durable >= 3 and report.acknowledged >= 3
        assert not (set(seed.event_ids) & set(report.dropped))

    run_async(scenario)


def test_domain_dlq_stream_name() -> None:
    assert domain_dlq_stream() == f"{_STREAM}.dlq"
