"""Pure planning core (Phase 50; Vol 6 §4, TITAN-WFL-015/017/018/019). No DB, no
Docker, no clock — the sprint planner, milestone state machine, estimation
correction, and blocker detector/ladder as fixture-driven unit tests."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from titan.planning import blockers, estimation, milestones
from titan.planning.planner import (
    PlanEdge,
    PlanningError,
    PlanTask,
    plan_sprints,
    verify_plan,
)

# --- WFL-015: the sprint planner ---------------------------------------------


def _skeleton_backlog() -> tuple[list[PlanTask], list[PlanEdge]]:
    """A well-formed walking-skeleton backlog: a thin db←backend←frontend←deploy
    chain plus two follow-on feature tasks."""
    tasks = [
        PlanTask(id="db", role="database", priority=1),
        PlanTask(id="be", role="backend", priority=1),
        PlanTask(id="fe", role="frontend", priority=1),
        PlanTask(id="deploy", role="devops", priority=1),
        PlanTask(id="feat_be", role="backend", priority=2),
        PlanTask(id="feat_fe", role="frontend", priority=2),
    ]
    edges = [
        PlanEdge("be", "db"),
        PlanEdge("fe", "be"),
        PlanEdge("deploy", "fe"),
        PlanEdge("feat_be", "be"),
        PlanEdge("feat_fe", "feat_be"),
    ]
    return tasks, edges


def test_plan_is_walking_skeleton_first_and_dag_ordered() -> None:
    tasks, edges = _skeleton_backlog()
    plan = plan_sprints(tasks, edges, parallel_capacity=4)

    assert verify_plan(plan, edges, parallel_capacity=4) == []
    sprint_of = {t.id: t.sprint_number for t in plan.tasks}
    # Sprint 1 is the skeleton: one task per engineering role + a deploy task.
    assert sprint_of["db"] == sprint_of["be"] == sprint_of["fe"] == sprint_of["deploy"] == 1
    assert plan.sprints[0].walking_skeleton is True
    roles_in_1 = {t.role for t in plan.tasks if t.sprint_number == 1}
    assert {"backend", "frontend", "database"} <= roles_in_1
    assert "devops" in roles_in_1
    # No task precedes any of its dependencies.
    for edge in edges:
        assert sprint_of[edge.depends_on_id] <= sprint_of[edge.task_id]
    # The follow-on features land in later sprints, after their deps.
    assert sprint_of["feat_be"] >= 2
    assert sprint_of["feat_fe"] > sprint_of["feat_be"]


def test_capacity_is_respected_and_sprints_are_non_empty() -> None:
    tasks, edges = _skeleton_backlog()
    plan = plan_sprints(tasks, edges, parallel_capacity=4)
    for sprint in plan.sprints:
        assert 1 <= len(sprint.task_ids) <= 4  # non-empty (releasable) and within capacity


def test_cycle_is_rejected() -> None:
    tasks, edges = _skeleton_backlog()
    edges.append(PlanEdge("be", "feat_fe"))  # be -> feat_fe -> feat_be -> be
    with pytest.raises(PlanningError, match="cycle"):
        plan_sprints(tasks, edges, parallel_capacity=4)


def test_missing_engineering_role_is_rejected() -> None:
    tasks = [
        PlanTask(id="be", role="backend"),
        PlanTask(id="fe", role="frontend"),
        PlanTask(id="deploy", role="devops"),
    ]
    with pytest.raises(PlanningError, match="role 'database'"):
        plan_sprints(tasks, [], parallel_capacity=4)


def test_missing_deploy_task_is_rejected() -> None:
    tasks = [
        PlanTask(id="db", role="database"),
        PlanTask(id="be", role="backend"),
        PlanTask(id="fe", role="frontend"),
    ]
    with pytest.raises(PlanningError, match="deploy"):
        plan_sprints(tasks, [], parallel_capacity=4)


def test_skeleton_over_capacity_is_rejected() -> None:
    tasks, edges = _skeleton_backlog()
    with pytest.raises(PlanningError, match="exceeds parallel capacity"):
        plan_sprints(tasks, edges, parallel_capacity=3)  # skeleton needs 4


def test_verify_plan_catches_a_hand_broken_plan() -> None:
    tasks, edges = _skeleton_backlog()
    plan = plan_sprints(tasks, edges, parallel_capacity=4)
    # Move a feature task into sprint 1 illegally, breaking a dependency order.
    from titan.planning.planner import ScheduledTask

    broken_tasks = tuple(
        ScheduledTask(
            id=t.id,
            role=t.role,
            sprint_number=1 if t.id == "feat_fe" else t.sprint_number,
            estimate_days=t.estimate_days,
            story_id=t.story_id,
            title=t.title,
            priority=t.priority,
        )
        for t in plan.tasks
    )
    from titan.planning.planner import SprintPlan

    violations = verify_plan(
        SprintPlan(sprints=plan.sprints, tasks=broken_tasks), edges, parallel_capacity=4
    )
    assert any("precedes its dependency" in v for v in violations)


# --- WFL-018: milestone tracking ---------------------------------------------


def _m2_signals(**overrides: object) -> milestones.MilestoneSignals:
    base: dict[str, Any] = dict(
        brief_accepted=True,
        backlog_accepted=True,
        skeleton_deployed=True,
        hello_path_green=True,
        must_stories_done=True,
        g6_g7_passed=True,
        open_p0_count=0,
    )
    base.update(overrides)
    return milestones.MilestoneSignals(**base)


def test_current_milestone_is_highest_contiguous_dod() -> None:
    assert milestones.current_milestone(milestones.MilestoneSignals()) == milestones.NONE
    assert milestones.current_milestone(_m2_signals()) == "M2"
    # A gap breaks the chain: M0 holds but M1 does not ⇒ current is M0, even if M2's
    # own facts happen to hold.
    gapped = _m2_signals(skeleton_deployed=False)
    assert milestones.current_milestone(gapped) == "M0"


def test_new_p0_after_m2_regresses_and_reopens_engineering() -> None:
    outcome = milestones.evaluate("M2", _m2_signals(open_p0_count=1))
    assert outcome.current == "M1"
    assert outcome.regressed is True
    assert outcome.failed_milestone == "M2"
    assert outcome.reopen_stage == "engineering"


def test_forward_progress_is_not_a_regression() -> None:
    outcome = milestones.evaluate("M1", _m2_signals())
    assert outcome.current == "M2"
    assert outcome.regressed is False
    assert outcome.reopen_stage is None


# --- WFL-017: estimation feedback --------------------------------------------


def test_correction_factor_is_median_ratio_over_min_samples() -> None:
    samples = [
        estimation.EstimateActual(estimate=2, actual=4),  # 2.0
        estimation.EstimateActual(estimate=3, actual=3),  # 1.0
        estimation.EstimateActual(estimate=5, actual=7.5),  # 1.5
    ]
    assert estimation.correction_factor(samples) == 1.5
    assert estimation.recalibrate(4, 1.5) == 6.0


def test_correction_factor_below_min_samples_is_neutral() -> None:
    samples = [estimation.EstimateActual(estimate=2, actual=10)]
    assert estimation.correction_factor(samples) == 1.0  # too little history
    assert estimation.recalibrate(3, 0.0) == 3.0  # degenerate factor is a no-op


# --- WFL-019: blocker detection + ladder -------------------------------------

_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def _snap(**kw: object) -> blockers.TaskSnapshot:
    base: dict[str, object] = dict(
        task_id="t1",
        status="in_progress",
        estimate_points=2,
        updated_at=_NOW - timedelta(minutes=5),
    )
    base.update(kw)
    return blockers.TaskSnapshot(**base)  # type: ignore[arg-type]


def test_stall_detected_past_two_x_estimate() -> None:
    # estimate 2 points × 4h/point = 8h; 2× ⇒ 16h stall threshold.
    fresh = _snap(updated_at=_NOW - timedelta(hours=10))
    stalled = _snap(updated_at=_NOW - timedelta(hours=20))
    args: dict[str, Any] = dict(stall_multiplier=2.0, hours_per_point=4.0, default_points=2)
    assert blockers.detect_block(fresh, _NOW, **args) is None
    assert blockers.detect_block(stalled, _NOW, **args) == blockers.BLOCK_STALLED


def test_definitive_signals_win_over_stall_and_terminal_states_are_ignored() -> None:
    args: dict[str, Any] = dict(stall_multiplier=2.0, hours_per_point=4.0, default_points=2)
    dep = _snap(dependency_failed=True)
    assert blockers.detect_block(dep, _NOW, **args) == blockers.BLOCK_DEP_FAILED
    clar = _snap(awaiting_clarification=True)
    assert blockers.detect_block(clar, _NOW, **args) == blockers.BLOCK_CLARIFICATION
    esc = _snap(loop_escalation=True)
    assert blockers.detect_block(esc, _NOW, **args) == blockers.BLOCK_LOOP_ESCALATION
    done = _snap(status="done", updated_at=_NOW - timedelta(days=30))
    assert blockers.detect_block(done, _NOW, **args) is None


def test_recovery_ladder_is_strictly_ordered_and_terminal() -> None:
    assert blockers.next_rung(None) == blockers.RUNG_DEPENDENCY_RECHECK
    assert blockers.next_rung(blockers.RUNG_DEPENDENCY_RECHECK) == blockers.RUNG_REASSIGN
    assert blockers.next_rung(blockers.RUNG_REASSIGN) == blockers.RUNG_DECOMPOSE
    assert blockers.next_rung(blockers.RUNG_DECOMPOSE) == blockers.RUNG_HUMAN
    assert blockers.next_rung(blockers.RUNG_HUMAN) == blockers.RUNG_HUMAN  # terminal
    assert blockers.is_terminal_rung(blockers.RUNG_HUMAN) is True
