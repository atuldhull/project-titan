"""A/B deployments + automatic rollback (Phase 86, Vol 10 §2/§6, EVO-003/018).

Proves the DoD anchor 2: a deployment whose challenger is artificially degraded (its arm
scores fall below the KPI floor) is auto-rolled-back to the champion by the monitor, and
the rollback emits the Prometheus counter, an audit event, and an incident RCR.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from tests.test_evolution_scorecard import _seed_project_task
from titan.audit.service import build_audit_client
from titan.db import uuid7
from titan.evolution import ab, rollback
from titan.observability.metrics import EVOLUTION_ROLLBACKS

pytestmark = pytest.mark.schema

_DIMS = {d: 40 for d in ("correctness", "first_pass_quality", "efficiency", "security")}


async def _seed_task(engine: Any, project_id: uuid.UUID) -> uuid.UUID:
    tid = uuid7()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tasks (id, project_id, title, spec, status, priority) "
                "VALUES (:id, :pid, 'ab', '', 'done', 3)"
            ),
            {"id": tid, "pid": project_id},
        )
    return tid


def test_degraded_challenger_is_auto_rolled_back(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid, _ = await _seed_project_task(migrated_engine, with_run=False)
        agent = f"agent_{uuid.uuid4().hex[:8]}"
        ep = f"EP-{uuid.uuid4().hex[:8]}"
        dep = await rollback.create_deployment(
            migrated_engine,
            ep=ep,
            agent_id=agent,
            mode="shadow",
            champion_ref="champ-sha",
            challenger_ref="chal-sha",
            kpi_floor=70.0,
            min_samples=3,
            observation_window_s=300,
        )
        assert dep is not None
        dep_id = uuid.UUID(dep["id"])

        # Chaos: the challenger produces 3 floor-breaching arm scores (40 << floor 70).
        for _ in range(3):
            tid = await _seed_task(migrated_engine, pid)
            inserted = await ab.record_arm(
                migrated_engine,
                deployment_id=dep_id,
                task_id=tid,
                arm="challenger",
                mode="shadow",
                agent_id=agent,
                task_class="code",
                composite_score=40.0,
                dimensions=_DIMS,
                prompt_sha="chal-sha",
                model_id="mock/eval",
            )
            assert inserted is True

        # The detector sees the breach...
        decision = await rollback.evaluate_deployment(migrated_engine, dep)
        assert decision.breached and decision.n == 3

        before = EVOLUTION_ROLLBACKS.labels(agent=agent, mode="shadow")._value.get()
        audit = build_audit_client(migrated_engine)
        rolled = await rollback.scan_and_rollback(migrated_engine, audit)
        assert rolled >= 1

        # The deployment is reverted...
        after = await rollback.get_deployment(migrated_engine, dep_id)
        assert after is not None and after["status"] == "rolled_back"
        assert "< floor" in after["rollback_reason"]
        # ...the Prometheus counter incremented...
        assert EVOLUTION_ROLLBACKS.labels(agent=agent, mode="shadow")._value.get() == before + 1

        async with migrated_engine.connect() as conn:
            # ...an audit event was written...
            audit_hit = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM audit_events WHERE action = 'evolution.rollback' "
                        "AND resource = :res"
                    ),
                    {"res": f"deployment:{dep['id']}"},
                )
            ).first()
            # ...and an incident RCR was opened.
            rcr_hit = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM root_cause_records WHERE signature = :sig "
                        "AND status = 'open'"
                    ),
                    {"sig": f"rollback:{ep}:{agent}"},
                )
            ).first()
        assert audit_hit is not None
        assert rcr_hit is not None

    run_async(scenario)


def test_healthy_challenger_is_not_rolled_back(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid, _ = await _seed_project_task(migrated_engine, with_run=False)
        agent = f"agent_{uuid.uuid4().hex[:8]}"
        dep = await rollback.create_deployment(
            migrated_engine,
            ep=f"EP-{uuid.uuid4().hex[:8]}",
            agent_id=agent,
            mode="shadow",
            champion_ref="c",
            challenger_ref="ch",
            kpi_floor=70.0,
            min_samples=3,
        )
        assert dep is not None
        for _ in range(3):
            tid = await _seed_task(migrated_engine, pid)
            await ab.record_arm(
                migrated_engine,
                deployment_id=uuid.UUID(dep["id"]),
                task_id=tid,
                arm="challenger",
                mode="shadow",
                agent_id=agent,
                task_class="code",
                composite_score=88.0,  # above the floor
                dimensions={d: 88 for d in _DIMS},
                prompt_sha="ch",
                model_id="m",
            )
        audit = build_audit_client(migrated_engine)
        assert await rollback.scan_and_rollback(migrated_engine, audit) == 0
        after = await rollback.get_deployment(migrated_engine, uuid.UUID(dep["id"]))
        assert after is not None and after["status"] == "shadow"

    run_async(scenario)


def test_one_active_deployment_per_agent(migrated_engine: Any) -> None:
    async def scenario() -> None:
        agent = f"agent_{uuid.uuid4().hex[:8]}"
        first = await rollback.create_deployment(
            migrated_engine,
            ep="EP-1",
            agent_id=agent,
            mode="shadow",
            champion_ref="c",
            challenger_ref="ch",
            kpi_floor=70.0,
        )
        assert first is not None
        dup = await rollback.create_deployment(
            migrated_engine,
            ep="EP-2",
            agent_id=agent,
            mode="canary",
            champion_ref="c",
            challenger_ref="ch2",
            kpi_floor=70.0,
            canary_pct=10,
        )
        assert dup is None  # the partial unique index blocks a second active deployment

    run_async(scenario)


def test_rollback_is_idempotent(migrated_engine: Any) -> None:
    async def scenario() -> None:
        agent = f"agent_{uuid.uuid4().hex[:8]}"
        dep = await rollback.create_deployment(
            migrated_engine,
            ep="EP-X",
            agent_id=agent,
            mode="shadow",
            champion_ref="c",
            challenger_ref="ch",
            kpi_floor=70.0,
        )
        assert dep is not None
        audit = build_audit_client(migrated_engine)
        first = await rollback.rollback_deployment(migrated_engine, audit, dep, reason="manual")
        assert first is not None and first["status"] == "rolled_back"
        # A second rollback of the same (already reverted) deployment is a no-op.
        assert (
            await rollback.rollback_deployment(migrated_engine, audit, dep, reason="again") is None
        )

    run_async(scenario)
