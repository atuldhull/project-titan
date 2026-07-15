"""The chaos suite: kill each service mid-project and prove recovery (TITAN-FND-
052 / TITAN-FND-007, DoD items 1 & 2). Schema-marked (test DB) + fakeredis for
real Redis-Streams semantics, kept in-process so the destructive ``kill_redis``
flush never touches shared infrastructure."""

from typing import Any

import fakeredis.aioredis
import pytest

from tests.conftest import run_async
from titan.chaos import ChaosHarness, KillTarget
from titan.chaos.model import RESUME_BUDGET_S
from titan.events.redis_bus import RedisStreamBus

pytestmark = pytest.mark.schema


def _harness(engine: Any) -> ChaosHarness:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return ChaosHarness(engine, RedisStreamBus(redis), redis)


def test_kill_orchestrator_redrains_and_resumes(migrated_engine: Any) -> None:
    """TITAN-FND-007: crash between commit and publish → restart re-drains the
    outbox, history intact, project resumes."""

    async def scenario() -> None:
        harness = _harness(migrated_engine)
        seed = await harness.seed(events=4)
        result = await harness.kill_orchestrator(seed)
        assert result.target == KillTarget.ORCHESTRATOR
        assert result.acknowledged_loss == 0 and result.duplicated == 0
        assert result.redelivered == 4 and result.history_intact
        assert result.resume_seconds <= RESUME_BUDGET_S and result.survived

    run_async(scenario)


def test_kill_worker_reclaims_and_reprocesses(migrated_engine: Any) -> None:
    """A worker that consumed but never acked: the replacement reclaims the
    idle-pending entries and reprocesses them idempotently (at-least-once)."""

    async def scenario() -> None:
        harness = _harness(migrated_engine)
        seed = await harness.seed(events=4)
        result = await harness.kill_worker(seed)
        assert result.acknowledged_loss == 0
        assert result.redelivered == 4  # all in-flight work redelivered
        assert result.duplicated == 0 and result.survived

    run_async(scenario)


def test_kill_postgres_is_atomic_and_durable(migrated_engine: Any) -> None:
    """A torn write rolls back with no partial state or orphan event; a committed
    event survives the restart."""

    async def scenario() -> None:
        harness = _harness(migrated_engine)
        seed = await harness.seed()
        result = await harness.kill_postgres(seed)
        assert result.target == KillTarget.POSTGRES
        assert result.acknowledged_loss == 0 and result.duplicated == 0
        assert result.history_intact and result.survived

    run_async(scenario)


def test_kill_backend_api_reconnect_replays_the_gap(migrated_engine: Any) -> None:
    """The WS connection drops; on reconnect the per-project buffer replays every
    frame after the client's cursor, in seq order — live delivery resumes."""

    async def scenario() -> None:
        harness = _harness(migrated_engine)
        seed = await harness.seed()
        result = await harness.kill_backend_api(seed)
        assert result.target == KillTarget.BACKEND_API
        assert result.redelivered == 3  # frames 4,5,6 after a last_seq of frame 3
        assert result.history_intact and result.survived

    run_async(scenario)


def test_kill_redis_replays_from_outbox(migrated_engine: Any) -> None:
    """Total queue data loss: the outbox (system of record) replays every
    unacknowledged event so nothing committed is lost."""

    async def scenario() -> None:
        harness = _harness(migrated_engine)
        seed = await harness.seed(events=4)
        result = await harness.kill_redis(seed)
        assert result.target == KillTarget.REDIS
        assert result.acknowledged_loss == 0 and result.duplicated == 0
        assert result.redelivered == 2  # the two unconsumed-before-wipe events
        assert result.survived

    run_async(scenario)


def test_full_sweep_reconciles_zero_acknowledged_loss(migrated_engine: Any) -> None:
    """The headline DoD: kill every service in turn, then reconcile — zero
    acknowledged-task loss, 100% crash recovery."""

    async def scenario() -> None:
        harness = _harness(migrated_engine)
        report = await harness.run_all()
        assert len(report.results) == 5
        assert {r.target for r in report.results} == set(KillTarget)
        assert report.reconciliation is not None
        assert report.reconciliation.dropped == []
        assert report.reconciliation.acknowledged_task_loss == 0
        assert report.reconciliation.orphaned == []
        assert report.crash_recovery_rate == 1.0 and report.all_passed

    run_async(scenario)
