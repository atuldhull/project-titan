"""A/B assignment + arm recording + sequential analysis (Phase 86, EVO-018)."""

import uuid
from typing import Any

import pytest

from tests.conftest import run_async
from tests.test_evolution_rollback import _seed_task
from tests.test_evolution_scorecard import _seed_project_task
from titan.evolution import ab, rollback

pytestmark_db = pytest.mark.schema


def test_ab_bucket_is_deterministic_and_bounded() -> None:
    tid = uuid.uuid4()
    # Same task + pct → same answer every call.
    assert ab.ab_bucket(tid, 50) == ab.ab_bucket(tid, 50)
    assert ab.ab_bucket(tid, 0) is False  # 0% → never
    assert ab.ab_bucket(tid, 100) is True  # 100% → always
    # Roughly `pct`% of a large sample lands in the canary cohort.
    ids = [uuid.uuid4() for _ in range(2000)]
    hits = sum(1 for i in ids if ab.ab_bucket(i, 10))
    assert 50 <= hits <= 350  # ~10% of 2000, generous bounds for hash noise


def test_assigned_arms() -> None:
    tid = uuid.uuid4()
    assert ab.assigned_arms("shadow", 0, tid) == ["champion", "challenger"]
    # canary: the cohort gets the challenger, everyone else the champion.
    arms = ab.assigned_arms("canary", 100, tid)
    assert arms == ["challenger"]
    assert ab.assigned_arms("canary", 0, tid) == ["champion"]


def test_sequential_test_recommendations() -> None:
    # Undersampled → continue.
    assert ab.sequential_test([90, 90], [50], min_samples=5).recommendation == "continue"
    # Clear win → promote.
    champ = [70.0] * 8
    winner = [90.0] * 8
    assert ab.sequential_test(champ, winner, min_samples=5).recommendation == "promote"
    # Clear loss → abort.
    loser = [40.0] * 8
    assert ab.sequential_test(champ, loser, min_samples=5).recommendation == "abort"
    # No real difference → continue.
    assert ab.sequential_test(champ, [70.0] * 8, min_samples=5).recommendation == "continue"


@pytestmark_db
def test_record_arm_idempotent_and_analysis(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid, _ = await _seed_project_task(migrated_engine, with_run=False)
        agent = f"agent_{uuid.uuid4().hex[:8]}"
        dep = await rollback.create_deployment(
            migrated_engine,
            ep="EP-ab",
            agent_id=agent,
            mode="shadow",
            champion_ref="c",
            challenger_ref="ch",
            kpi_floor=50.0,
            min_samples=3,
        )
        assert dep is not None
        dep_id = uuid.UUID(dep["id"])
        tid = await _seed_task(migrated_engine, pid)
        dims = {"correctness": 80}
        first = await ab.record_arm(
            migrated_engine,
            deployment_id=dep_id,
            task_id=tid,
            arm="champion",
            mode="shadow",
            agent_id=agent,
            task_class="code",
            composite_score=80.0,
            dimensions=dims,
            prompt_sha="c",
            model_id="m",
        )
        # A redelivery of the same (deployment, task, arm) is a no-op.
        dup = await ab.record_arm(
            migrated_engine,
            deployment_id=dep_id,
            task_id=tid,
            arm="champion",
            mode="shadow",
            agent_id=agent,
            task_class="code",
            composite_score=80.0,
            dimensions=dims,
            prompt_sha="c",
            model_id="m",
        )
        assert first is True and dup is False
        scores = await ab.arm_scores(migrated_engine, dep_id)
        assert scores["champion"] == [80.0]

    run_async(scenario)
