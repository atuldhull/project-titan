"""Persisted planning mechanics (Phase 50; Vol 6 §4). Schema-marked — needs the
test DB. Proves the DoD behaviors: three ceremony records per closed sprint with
estimation feedback, a milestone regression that reverts + reopens the stage, and a
synthetically stalled task detected + recovered within one injected-clock tick."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.clarification import service as clarification
from titan.planning import service as planning

pytestmark = pytest.mark.schema


async def _make_project(engine: Any) -> uuid.UUID:
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'creator', 'local', :s)"
            ),
            {"id": uid, "e": f"u-{uid.hex[:12]}@t.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :n, 'P', 'b', :u)"
            ),
            {"id": pid, "n": f"p-{pid.hex[:12]}", "u": uid},
        )
    return pid


async def _insert_task(
    engine: Any, pid: uuid.UUID, *, status: str, points: int | None = 2, priority: int = 3
) -> uuid.UUID:
    tid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tasks (id, project_id, title, status, priority, estimate_points) "
                "VALUES (:id, :pid, 't', :st, :pr, :pts)"
            ),
            {"id": tid, "pid": pid, "st": status, "pr": priority, "pts": points},
        )
    return tid


async def _events(engine: Any, event_type: str, pid: uuid.UUID) -> int:
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT count(*) FROM outbox WHERE payload->'data'->>'type' = :t "
                "AND payload->'data'->'ceremony'->>'project_id' = :pid"
            ),
            {"t": event_type, "pid": str(pid)},
        )
        return int(row.scalar_one())


# --- WFL-016 + 017: ceremonies + estimation feedback -------------------------


def test_closed_sprint_has_all_three_ceremonies_and_recalibrates(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))

    def scenario() -> tuple[list[Any], float, float]:
        open_out = run_async(
            lambda: planning.open_sprint(
                migrated_engine,
                pid,
                planning.OpenSprintIn(
                    sprint_number=1,
                    goal="Walking skeleton",
                    committed_stories=["S1", "S2"],
                    risks=["proxy flakiness"],
                ),
                actor="agent.pm.1",
            )
        )
        assert open_out.kind == "planning"
        close_out = run_async(
            lambda: planning.close_sprint(
                migrated_engine,
                pid,
                planning.CloseSprintIn(
                    sprint_number=1,
                    demo_links=["http://demo/1"],
                    velocity_estimate=10.0,
                    velocity_actual=15.0,
                    retrospective_changes=["smaller tasks", "earlier reviews"],
                    estimate_actuals=[
                        planning.EstimateActualIn(estimate=2, actual=4),
                        planning.EstimateActualIn(estimate=3, actual=3),
                        planning.EstimateActualIn(estimate=5, actual=7.5),
                    ],
                ),
                actor="agent.pm.1",
            )
        )

        async def read() -> tuple[list[Any], float]:
            async with migrated_engine.connect() as conn:
                cers = await planning.list_ceremonies(conn, pid)
                factor = await planning.latest_correction_factor(conn, pid)
            return cers, factor

        cers, factor = run_async(read)
        return cers, close_out.correction_factor, factor

    ceremonies, close_factor, read_factor = scenario()
    kinds = {c.kind for c in ceremonies}
    assert kinds == {"planning", "review", "retrospective"}  # all three persisted
    assert close_factor == 1.5  # median(2.0, 1.0, 1.5)
    assert read_factor == 1.5  # recalibration reads the factor back
    retro = next(c for c in ceremonies if c.kind == "retrospective")
    assert retro.memory_record_id is not None  # fed to organization memory
    assert run_async(lambda: _events(migrated_engine, "planning.ceremony", pid)) == 3


# --- WFL-018: milestone regression -------------------------------------------

_M2 = dict(
    brief_accepted=True,
    backlog_accepted=True,
    skeleton_deployed=True,
    hello_path_green=True,
    must_stories_done=True,
    g6_g7_passed=True,
)


def test_new_p0_after_m2_reverts_milestone_and_opens_reopen_defect(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))

    def scenario() -> tuple[Any, Any, str]:
        # Advance to M2.
        first = run_async(
            lambda: planning.sync_milestone(
                migrated_engine, pid, planning.MilestoneSignalsIn(**_M2), actor="agent.pm.1"
            )
        )
        # A new P0 appears — M2's DoD no longer holds.
        regressed = run_async(
            lambda: planning.sync_milestone(
                migrated_engine,
                pid,
                planning.MilestoneSignalsIn(**_M2, open_p0_count=1),
                actor="agent.pm.1",
            )
        )

        async def read_state() -> str:
            async with migrated_engine.connect() as conn:
                state = await planning.get_milestone(conn, pid)
            return state.current_milestone

        return first, regressed, run_async(read_state)

    first, regressed, persisted = scenario()
    assert first.current == "M2" and first.regressed is False
    assert regressed.current == "M1" and regressed.regressed is True
    assert regressed.failed_milestone == "M2"
    assert regressed.reopen_stage == "engineering"
    assert regressed.defect_task_id is not None
    assert persisted == "M1"  # the dashboard state reverted

    async def defect_ok() -> bool:
        async with migrated_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT priority, project_id FROM tasks WHERE id = :id"),
                {"id": regressed.defect_task_id},
            )
            found = row.one_or_none()
            return found is not None and found.priority == 1 and found.project_id == pid

    assert run_async(defect_ok) is True  # the reopen defect task exists


# --- WFL-019: blocker detection + recovery within one injected-clock tick ----


def test_stalled_task_detected_and_recovery_attempted_in_one_tick(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    tid = run_async(lambda: _insert_task(migrated_engine, pid, status="in_progress", points=2))
    # estimate 2pt × 4h × 2× ⇒ 16h stall threshold; inject a clock 100h ahead.
    future = datetime.now(UTC) + timedelta(hours=100)

    result = run_async(lambda: planning.sweep_blockers(migrated_engine, project_id=pid, now=future))
    assert result.detected == 1  # detected in this tick

    async def read_blocks() -> list[Any]:
        async with migrated_engine.connect() as conn:
            return await planning.list_blocks(conn, pid)

    blocks = run_async(read_blocks)
    assert len(blocks) == 1
    assert blocks[0].task_id == tid
    assert blocks[0].reason == "stalled"
    assert blocks[0].rung == "dependency_recheck"  # first recovery rung attempted
    assert blocks[0].status == "open"

    async def blocker_events() -> int:
        async with migrated_engine.connect() as conn:
            row = await conn.execute(
                text(
                    "SELECT count(*) FROM outbox "
                    "WHERE payload->'data'->>'type' = 'planning.blocker' "
                    "AND payload->'data'->'blocker'->>'task_id' = :tid"
                ),
                {"tid": str(tid)},
            )
            return int(row.scalar_one())

    assert run_async(blocker_events) >= 1


def test_recovery_ladder_advances_one_rung_per_tick_then_escalates(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    run_async(lambda: _insert_task(migrated_engine, pid, status="in_progress", points=1))
    future = datetime.now(UTC) + timedelta(hours=100)

    def rung_after_ticks(n: int) -> tuple[str, str]:
        for _ in range(n):
            run_async(lambda: planning.sweep_blockers(migrated_engine, project_id=pid, now=future))

        async def read() -> tuple[str, str]:
            async with migrated_engine.connect() as conn:
                blocks = await planning.list_blocks(conn, pid)
            return str(blocks[0].rung), str(blocks[0].status)

        result: tuple[str, str] = run_async(read)
        return result

    assert rung_after_ticks(1) == ("dependency_recheck", "open")
    assert rung_after_ticks(1) == ("reassign", "open")
    assert rung_after_ticks(1) == ("decompose", "open")
    assert rung_after_ticks(1) == ("human_escalation", "escalated")
    # Terminal: further ticks do not advance past human escalation.
    assert rung_after_ticks(1) == ("human_escalation", "escalated")


def test_reassign_does_not_reset_the_stall_clock(migrated_engine: Any) -> None:
    """Regression guard: the reassign rung clears the assignee, which bumps
    tasks.updated_at via the row trigger. A naive re-detection off updated_at would
    then see the task as 'fresh' and spuriously recover the block, so it would never
    escalate. The ladder must advance on the persisting condition instead."""
    pid = run_async(lambda: _make_project(migrated_engine))
    run_async(lambda: _insert_task(migrated_engine, pid, status="in_progress", points=2))
    future = datetime.now(UTC) + timedelta(hours=100)

    # Tick 1 detects; tick 2 advances to reassign (clearing the assignee → updated_at
    # jumps to ~now).
    run_async(lambda: planning.sweep_blockers(migrated_engine, project_id=pid, now=future))
    run_async(lambda: planning.sweep_blockers(migrated_engine, project_id=pid, now=future))

    # Tick 3 uses REAL now (as the production worker does): the task's updated_at is
    # now recent, so a stall re-derived from updated_at would be false. The block must
    # still advance (to decompose), not recover.
    result = run_async(
        lambda: planning.sweep_blockers(migrated_engine, project_id=pid, now=datetime.now(UTC))
    )
    assert result.recovered == 0 and result.advanced == 1

    async def read() -> tuple[str, str]:
        async with migrated_engine.connect() as conn:
            blocks = await planning.list_blocks(conn, pid)
        return blocks[0].rung, blocks[0].status

    assert run_async(read) == ("decompose", "open")


def test_correction_factor_is_read_back_and_recalibrates_new_estimates(
    migrated_engine: Any,
) -> None:
    """WFL-017 end-to-end: a sprint's actuals yield a correction factor that a later
    plan reads back and applies to its raw estimates."""
    pid = run_async(lambda: _make_project(migrated_engine))
    run_async(
        lambda: planning.close_sprint(
            migrated_engine,
            pid,
            planning.CloseSprintIn(
                sprint_number=1,
                estimate_actuals=[
                    planning.EstimateActualIn(estimate=2, actual=4),
                    planning.EstimateActualIn(estimate=3, actual=3),
                    planning.EstimateActualIn(estimate=5, actual=7.5),
                ],
            ),
            actor="agent.pm.1",
        )
    )

    async def read_factor() -> float:
        async with migrated_engine.connect() as conn:
            return await planning.latest_correction_factor(conn, pid)

    factor = run_async(read_factor)
    assert factor == 1.5

    plan = planning.build_plan(
        planning.PlanRequest(
            tasks=[
                planning.PlanTaskIn(id="db", role="database", estimate_days=1.0),
                planning.PlanTaskIn(id="be", role="backend", estimate_days=1.0),
                planning.PlanTaskIn(id="fe", role="frontend", estimate_days=1.0),
                planning.PlanTaskIn(id="deploy", role="devops", estimate_days=1.0),
            ],
            edges=[],
            parallel_capacity=4,
            correction_factor=factor,
        )
    )
    assert all(t.estimate_days == 1.5 for t in plan.tasks)  # 1.0 raw × 1.5 factor


def test_dependency_failed_is_detected_without_a_stall(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    dep = run_async(lambda: _insert_task(migrated_engine, pid, status="failed"))
    tid = run_async(lambda: _insert_task(migrated_engine, pid, status="in_progress"))

    async def link() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO task_dependencies (task_id, depends_on_id) VALUES (:t, :d)"),
                {"t": tid, "d": dep},
            )

    run_async(link)
    # now == real now (not stalled); the failed dependency is the definitive signal.
    result = run_async(
        lambda: planning.sweep_blockers(migrated_engine, project_id=pid, now=datetime.now(UTC))
    )
    assert result.detected == 1

    async def read() -> Any:
        async with migrated_engine.connect() as conn:
            blocks = await planning.list_blocks(conn, pid)
        return blocks

    blocks = run_async(read)
    assert blocks[0].task_id == tid and blocks[0].reason == "dependency_failed"


def test_block_recovers_when_task_leaves_the_blocked_condition(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    tid = run_async(lambda: _insert_task(migrated_engine, pid, status="in_progress", points=2))
    future = datetime.now(UTC) + timedelta(hours=100)
    run_async(lambda: planning.sweep_blockers(migrated_engine, project_id=pid, now=future))

    # The task completes; the next sweep must recover its open block.
    async def finish() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(text("UPDATE tasks SET status = 'done' WHERE id = :id"), {"id": tid})

    run_async(finish)
    result = run_async(lambda: planning.sweep_blockers(migrated_engine, project_id=pid, now=future))
    assert result.recovered == 1

    async def read() -> str:
        async with migrated_engine.connect() as conn:
            blocks = await planning.list_blocks(conn, pid)
        return blocks[0].status

    assert run_async(read) == "recovered"


def test_compute_signals_reads_the_gate_ledger_and_open_p0s(migrated_engine: Any) -> None:
    """The best-effort signal derivation reads the handoffs gate ledger (latest
    verdict per stage) and counts open critical tasks."""
    pid = run_async(lambda: _make_project(migrated_engine))
    run_async(lambda: _insert_task(migrated_engine, pid, status="in_progress", priority=1))

    async def seed_and_compute() -> Any:
        async with migrated_engine.begin() as conn:
            for stage, verdict in [("ceo_vision", "pass"), ("pm_backlog", "pass")]:
                await conn.execute(
                    text(
                        "INSERT INTO gate_evaluations "
                        "(id, project_id, stage, attempt, verdict, evaluator_agent, "
                        " evaluator_model, prompt_version) "
                        "VALUES (:id, :pid, :st, 1, :v, 'rules', 'rules', 'v1')"
                    ),
                    {"id": uuid.uuid4(), "pid": pid, "st": stage, "v": verdict},
                )
        async with migrated_engine.connect() as conn:
            return await planning.compute_signals(conn, pid)

    signals = run_async(seed_and_compute)
    assert signals.brief_accepted is True  # ceo_vision gate passed
    assert signals.backlog_accepted is True  # pm_backlog gate passed
    assert signals.g6_g7_passed is False  # engineering/qa not evaluated
    assert signals.open_p0_count == 1  # the open priority-1 task


def test_compute_signals_excludes_milestone_reopen_defects_from_p0(migrated_engine: Any) -> None:
    """The reopen defect a regression opens is priority-1, but must NOT count toward
    the P0 signal — else a later-milestone regression self-cascades down to M2."""
    pid = run_async(lambda: _make_project(migrated_engine))
    # Drive a regression M2 -> M1 so a "Milestone regression …" reopen defect is opened.
    run_async(
        lambda: planning.sync_milestone(
            migrated_engine, pid, planning.MilestoneSignalsIn(**_M2), actor="agent.pm.1"
        )
    )
    run_async(
        lambda: planning.sync_milestone(
            migrated_engine,
            pid,
            planning.MilestoneSignalsIn(**_M2, open_p0_count=1),
            actor="agent.pm.1",
        )
    )

    async def compute() -> Any:
        async with migrated_engine.connect() as conn:
            return await planning.compute_signals(conn, pid)

    signals = run_async(compute)
    assert signals.open_p0_count == 0  # the reopen defect is excluded


# --- WFL-008: the H12 restatement of timed-out assumptions -------------------


def test_high_visibility_assumptions_surface_for_release(migrated_engine: Any) -> None:
    """The best-judgment assumptions a timed-out clarification proceeded on are read
    back for ReleaseApproval.assumptions_restated (TITAN-WFL-008 restatement at H12)."""
    pid = run_async(lambda: _make_project(migrated_engine))

    async def seed_and_read() -> list[str]:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO clarification_batches "
                    "(id, project_id, status, timeout_hours, deadline_at, resolution, "
                    " assumptions, opened_by) "
                    "VALUES (:id, :pid, 'timed_out', 24, now(), 'timeout_best_judgment', "
                    " CAST(:a AS jsonb), 'ceo')"
                ),
                {
                    "id": uuid.uuid4(),
                    "pid": pid,
                    "a": (
                        '[{"statement": "Use Stripe test mode", "source": "timeout", '
                        '"user_overridable": true, "high_visibility": true}, '
                        '{"statement": "SQLite for dev", "source": "canonical_default", '
                        '"user_overridable": true, "high_visibility": false}]'
                    ),
                },
            )
        async with migrated_engine.connect() as conn:
            return await clarification.assumptions_for_release(conn, pid)

    restated = run_async(seed_and_read)
    assert restated == ["Use Stripe test mode"]  # only the high-visibility one
