"""Public service interface of the ``planning`` module (Vol 6 §4, TITAN-WFL-015 …
019). Only this interface may be imported by other modules; Pydantic v2 DTOs — never
ORM rows — cross the boundary.

Five mechanics:

* **Sprint planner** (WFL-015) — :func:`build_plan` wraps the pure planner.
* **Ceremonies** (WFL-016) — :func:`open_sprint` / :func:`close_sprint` persist the
  planning/review/retrospective records; the retrospective is filed to organization
  memory as a lesson.
* **Estimation feedback** (WFL-017) — :func:`close_sprint` computes a correction
  factor from the sprint's actuals and caches it for :func:`latest_correction_factor`.
* **Milestone tracking** (WFL-018) — :func:`sync_milestone` evaluates the M0–M4 DoDs
  and, on a regression, reopens the relevant pipeline stage via a defect task.
* **Blocker ladder** (WFL-019) — :func:`sweep_blockers` detects blocked tasks and
  runs the recovery ladder one rung per ≤60 s tick (injected ``now`` for tests).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.events.service import STREAM_EVENTS_DOMAIN, write_event
from titan.gateway.problems import ProblemException, problem_type
from titan.memory.service import LessonIn, file_lesson
from titan.orchestrator import service as orchestrator
from titan.planning import blockers, estimation, milestones
from titan.planning import planner as planner_mod
from titan.planning import repository as repo
from titan.settings import get_settings

# --- event types -------------------------------------------------------------
CEREMONY_EVENT = "planning.ceremony"
MILESTONE_EVENT = "planning.milestone"
BLOCKER_EVENT = "planning.blocker"

# The title prefix of the defect task a milestone regression opens to reopen a stage.
# ``compute_signals`` excludes these from the P0 count so a later-milestone regression's
# own reopen defect cannot spuriously fail M2's "no open P0" clause and cascade down.
REOPEN_DEFECT_PREFIX = "Milestone regression"


def _bad_request(detail: str) -> ProblemException:
    return ProblemException(
        status=422, title="Invalid plan", detail=detail, problem_type=problem_type("validation")
    )


# --- WFL-015: the sprint planner (pure, exposed for the API) ----------------


class PlanTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1, max_length=100)
    estimate_days: float = Field(default=0.5, gt=0, le=5.0)
    story_id: str = Field(default="", max_length=100)
    title: str = Field(default="", max_length=500)
    priority: int = Field(default=3, ge=1, le=5)


class PlanEdgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=100)
    depends_on_id: str = Field(min_length=1, max_length=100)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[PlanTaskIn] = Field(min_length=1, max_length=2000)
    edges: list[PlanEdgeIn] = Field(default_factory=list, max_length=5000)
    parallel_capacity: int | None = Field(default=None, ge=1, le=100)
    # WFL-017 estimation feedback: raw estimates are recalibrated by this factor
    # (read from the project's latest retrospective via ``latest_correction_factor``).
    correction_factor: float = Field(default=1.0, gt=0, le=100)


class ScheduledTaskOut(BaseModel):
    id: str
    role: str
    sprint_number: int
    estimate_days: float
    story_id: str
    title: str
    priority: int


class PlannedSprintOut(BaseModel):
    number: int
    goal: str
    task_ids: list[str]
    walking_skeleton: bool


class SprintPlanOut(BaseModel):
    sprints: list[PlannedSprintOut]
    tasks: list[ScheduledTaskOut]


def build_plan(request: PlanRequest) -> SprintPlanOut:
    """Partition the request's TaskGraph into a walking-skeleton-first plan
    (WFL-015). Raises a 422 ProblemException on a cyclic/incomplete/over-capacity
    backlog."""
    capacity = request.parallel_capacity or get_settings().planning_parallel_capacity
    try:
        plan = planner_mod.plan_sprints(
            [
                planner_mod.PlanTask(
                    id=t.id,
                    role=t.role,
                    estimate_days=t.estimate_days,
                    story_id=t.story_id,
                    title=t.title,
                    priority=t.priority,
                )
                for t in request.tasks
            ],
            [
                planner_mod.PlanEdge(task_id=e.task_id, depends_on_id=e.depends_on_id)
                for e in request.edges
            ],
            parallel_capacity=capacity,
        )
    except planner_mod.PlanningError as exc:
        raise _bad_request(str(exc)) from exc
    return SprintPlanOut(
        sprints=[
            PlannedSprintOut(
                number=s.number,
                goal=s.goal,
                task_ids=list(s.task_ids),
                walking_skeleton=s.walking_skeleton,
            )
            for s in plan.sprints
        ],
        tasks=[
            ScheduledTaskOut(
                id=t.id,
                role=t.role,
                sprint_number=t.sprint_number,
                # WFL-017: recalibrate the raw estimate by the project's correction factor.
                estimate_days=estimation.recalibrate(t.estimate_days, request.correction_factor),
                story_id=t.story_id,
                title=t.title,
                priority=t.priority,
            )
            for t in plan.tasks
        ],
    )


# --- WFL-016 + 017: sprint ceremonies + estimation feedback -----------------


class SprintCeremonyOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    sprint_number: int
    kind: str
    body: dict[str, Any]
    memory_record_id: uuid.UUID | None = None
    created_at: datetime


class OpenSprintIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sprint_number: int = Field(ge=1)
    goal: str = Field(min_length=1, max_length=2000)
    committed_stories: list[str] = Field(default_factory=list, max_length=500)
    risks: list[str] = Field(default_factory=list, max_length=100)


class EstimateActualIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimate: float = Field(gt=0)
    actual: float = Field(ge=0)


class CloseSprintIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sprint_number: int = Field(ge=1)
    demo_links: list[str] = Field(default_factory=list, max_length=100)
    velocity_estimate: float = Field(default=0.0, ge=0)
    velocity_actual: float = Field(default=0.0, ge=0)
    retrospective_changes: list[str] = Field(default_factory=list, max_length=100)
    estimate_actuals: list[EstimateActualIn] = Field(default_factory=list, max_length=2000)


class CloseSprintResult(BaseModel):
    review: SprintCeremonyOut
    retrospective: SprintCeremonyOut
    correction_factor: float


def _ceremony_out(row: Row[Any]) -> SprintCeremonyOut:
    return SprintCeremonyOut(
        id=row.id,
        sprint_number=row.sprint_number,
        kind=row.kind,
        body=dict(row.body or {}),
        memory_record_id=row.memory_record_id,
        created_at=row.created_at,
    )


async def open_sprint(
    engine: AsyncEngine, project_id: uuid.UUID, data: OpenSprintIn, *, actor: str
) -> SprintCeremonyOut:
    """Persist the sprint-opening planning ceremony (goals, committed stories,
    risks) and emit its event (TITAN-WFL-016)."""
    body = {
        "goal": data.goal,
        "committed_stories": data.committed_stories,
        "risks": data.risks,
    }
    async with engine.begin() as conn:
        row = await repo.upsert_ceremony(
            conn,
            project_id,
            sprint_number=data.sprint_number,
            kind="planning",
            body=body,
            memory_record_id=None,
        )
        await _emit_ceremony(conn, project_id, data.sprint_number, "planning", actor)
    return _ceremony_out(row)


async def close_sprint(
    engine: AsyncEngine, project_id: uuid.UUID, data: CloseSprintIn, *, actor: str
) -> CloseSprintResult:
    """Persist the sprint-closing review + retrospective ceremonies, file the
    retrospective to organization memory as a lesson, and recalibrate estimates from
    the sprint's actuals (TITAN-WFL-016/017)."""
    factor = estimation.correction_factor(
        [
            estimation.EstimateActual(estimate=s.estimate, actual=s.actual)
            for s in data.estimate_actuals
        ]
    )
    mape = estimation.median_abs_pct_error(
        [
            estimation.EstimateActual(estimate=s.estimate, actual=s.actual)
            for s in data.estimate_actuals
        ]
    )
    # File the retrospective to organization memory (WFL-016: "fed to organization
    # memory"); the correction factor rides along so it is stored there too (WFL-017).
    changes = "; ".join(data.retrospective_changes) or "No process changes proposed this sprint."
    observation = f"Estimation correction factor {factor:.3f} (median abs pct err {mape:.1f}%)."
    context = (
        f"Sprint {data.sprint_number} closed; velocity {data.velocity_actual} "
        f"vs estimate {data.velocity_estimate}."
    )
    lesson = await file_lesson(
        engine,
        LessonIn(
            scope=f"project:{project_id}",
            title=f"Sprint {data.sprint_number} retrospective",
            context=context,
            observation=observation,
            root_cause="Aggregated from the sprint's completed-task actuals.",
            recommendation=changes,
            applicability="org-candidate",
        ),
        source_agent=actor,
        actor_agent=actor,
    )
    review_body = {
        "demo_links": data.demo_links,
        "velocity_estimate": data.velocity_estimate,
        "velocity_actual": data.velocity_actual,
    }
    retro_body = {
        "changes": data.retrospective_changes,
        "correction_factor": factor,
        "median_abs_pct_error": mape,
        "sample_count": len(data.estimate_actuals),
    }
    async with engine.begin() as conn:
        review = await repo.upsert_ceremony(
            conn,
            project_id,
            sprint_number=data.sprint_number,
            kind="review",
            body=review_body,
            memory_record_id=None,
        )
        retro = await repo.upsert_ceremony(
            conn,
            project_id,
            sprint_number=data.sprint_number,
            kind="retrospective",
            body=retro_body,
            memory_record_id=lesson.record_id,
        )
        await _emit_ceremony(conn, project_id, data.sprint_number, "review", actor)
        await _emit_ceremony(conn, project_id, data.sprint_number, "retrospective", actor)
    return CloseSprintResult(
        review=_ceremony_out(review),
        retrospective=_ceremony_out(retro),
        correction_factor=factor,
    )


async def latest_correction_factor(conn: AsyncConnection, project_id: uuid.UUID) -> float:
    """The most recent sprint's estimation correction factor for recalibrating the
    next sprint's story points (WFL-017); 1.0 if no retrospective has run yet."""
    rows = await repo.list_ceremonies(conn, project_id)
    retros = [r for r in rows if r.kind == "retrospective"]
    if not retros:
        return 1.0
    latest = max(retros, key=lambda r: r.sprint_number)
    return float((latest.body or {}).get("correction_factor", 1.0))


async def list_ceremonies(conn: AsyncConnection, project_id: uuid.UUID) -> list[SprintCeremonyOut]:
    return [_ceremony_out(r) for r in await repo.list_ceremonies(conn, project_id)]


async def _emit_ceremony(
    conn: AsyncConnection, project_id: uuid.UUID, sprint_number: int, kind: str, actor: str
) -> None:
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {
            "type": CEREMONY_EVENT,
            "ceremony": {
                "project_id": str(project_id),
                "sprint_number": sprint_number,
                "kind": kind,
            },
        },
        actor=actor,
    )


# --- WFL-018: milestone tracking --------------------------------------------


class MilestoneSignalsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_accepted: bool = False
    backlog_accepted: bool = False
    skeleton_deployed: bool = False
    hello_path_green: bool = False
    must_stories_done: bool = False
    g6_g7_passed: bool = False
    open_p0_count: int = Field(default=0, ge=0)
    g8_g9_passed: bool = False
    perf_budgets_met: bool = False
    debt_reviewed: bool = False
    g10_g12_passed: bool = False
    docs_live: bool = False
    user_notified: bool = False


class MilestoneStateOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: uuid.UUID
    current_milestone: str
    signals: dict[str, Any]
    updated_at: datetime


class MilestoneSyncResult(BaseModel):
    current: str
    previous: str
    regressed: bool
    failed_milestone: str | None
    reopen_stage: str | None
    defect_task_id: uuid.UUID | None


def _to_signals(data: MilestoneSignalsIn) -> milestones.MilestoneSignals:
    return milestones.MilestoneSignals(**data.model_dump())


async def sync_milestone(
    engine: AsyncEngine, project_id: uuid.UUID, data: MilestoneSignalsIn, *, actor: str
) -> MilestoneSyncResult:
    """Evaluate the current milestone from ``data`` and persist it. On a regression
    (the state dropped below the previous milestone), reopen the relevant pipeline
    stage by opening a defect task and emit a milestone event (TITAN-WFL-018)."""
    async with engine.begin() as conn:
        existing = await repo.get_milestone_state(conn, project_id)
        previous = existing.current_milestone if existing else milestones.NONE
        outcome = milestones.evaluate(previous, _to_signals(data))
        await repo.upsert_milestone_state(
            conn,
            project_id,
            current_milestone=outcome.current,
            signals=data.model_dump(),
        )
        defect_task_id: uuid.UUID | None = None
        if outcome.regressed and outcome.reopen_stage is not None:
            # The pipeline machine is forward-only, so "reopen the stage" is a defect
            # task scoped to that stage (the Phase-49 G10 precedent), not a rewind.
            defect = await orchestrator.create_task(
                conn,
                project_id,
                orchestrator.TaskCreate(
                    title=(
                        f"{REOPEN_DEFECT_PREFIX} {previous}->{outcome.current}: "
                        f"reopen {outcome.reopen_stage}"
                    ),
                    spec=(
                        f"The project regressed from {previous} to {outcome.current} "
                        f"({outcome.failed_milestone} definition of done no longer holds). "
                        f"Reopen the {outcome.reopen_stage} stage and remediate."
                    ),
                    priority=1,
                ),
                actor=actor,
            )
            defect_task_id = defect.id
        await _emit_milestone(conn, project_id, outcome, actor)
    return MilestoneSyncResult(
        current=outcome.current,
        previous=outcome.previous,
        regressed=outcome.regressed,
        failed_milestone=outcome.failed_milestone,
        reopen_stage=outcome.reopen_stage,
        defect_task_id=defect_task_id,
    )


async def get_milestone(conn: AsyncConnection, project_id: uuid.UUID) -> MilestoneStateOut:
    """The project's current milestone (defaults to ``none`` before the first
    evaluation)."""
    row = await repo.get_milestone_state(conn, project_id)
    if row is None:
        now = datetime.now(UTC)
        return MilestoneStateOut(
            project_id=project_id, current_milestone=milestones.NONE, signals={}, updated_at=now
        )
    return MilestoneStateOut(
        project_id=row.project_id,
        current_milestone=row.current_milestone,
        signals=dict(row.signals or {}),
        updated_at=row.updated_at,
    )


async def compute_signals(conn: AsyncConnection, project_id: uuid.UUID) -> MilestoneSignalsIn:
    """Best-effort derivation of milestone signals from the gate ledger + open P0
    tasks. The gate-driven booleans are authoritative; softer signals (docs live,
    user notified) are proxied from their producing gate. Callers may override."""
    passed = await repo.stage_gate_passed(conn, project_id)
    # Exclude the milestone machinery's OWN reopen defects so a later-milestone
    # regression cannot self-referentially fail M2's "no open P0" clause.
    open_p0 = await repo.open_p0_count(conn, project_id, exclude_title_prefix=REOPEN_DEFECT_PREFIX)
    return MilestoneSignalsIn(
        brief_accepted=passed.get("ceo_vision", False),
        backlog_accepted=passed.get("pm_backlog", False),
        skeleton_deployed=passed.get("devops_deploy", False),
        hello_path_green=passed.get("devops_deploy", False),
        must_stories_done=passed.get("engineering", False) and passed.get("qa", False),
        g6_g7_passed=passed.get("engineering", False) and passed.get("qa", False),
        open_p0_count=open_p0,
        g8_g9_passed=passed.get("security", False) and passed.get("code_review", False),
        perf_budgets_met=passed.get("qa", False),
        debt_reviewed=passed.get("code_review", False),
        g10_g12_passed=passed.get("devops_deploy", False)
        and passed.get("documentation", False)
        and passed.get("ceo_release", False),
        docs_live=passed.get("documentation", False),
        user_notified=passed.get("ceo_release", False),
    )


async def _emit_milestone(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    outcome: milestones.MilestoneOutcome,
    actor: str,
) -> None:
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {
            "type": MILESTONE_EVENT,
            "milestone": {
                "project_id": str(project_id),
                "current": outcome.current,
                "previous": outcome.previous,
                "regressed": outcome.regressed,
                "reopen_stage": outcome.reopen_stage,
            },
        },
        actor=actor,
    )


# --- WFL-019: blocker detection + the recovery ladder -----------------------


class TaskBlockOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    task_id: uuid.UUID
    reason: str
    rung: str
    status: str
    detail: str
    detected_at: datetime
    resolved_at: datetime | None = None


class BlockerSweepResult(BaseModel):
    scanned: int
    detected: int
    advanced: int
    recovered: int
    escalated: int


def _block_out(row: Row[Any]) -> TaskBlockOut:
    return TaskBlockOut(
        id=row.id,
        task_id=row.task_id,
        reason=row.reason,
        rung=row.rung,
        status=row.status,
        detail=row.detail,
        detected_at=row.detected_at,
        resolved_at=row.resolved_at,
    )


async def list_blocks(conn: AsyncConnection, project_id: uuid.UUID) -> list[TaskBlockOut]:
    return [_block_out(r) for r in await repo.list_blocks(conn, project_id)]


async def sweep_blockers(
    engine: AsyncEngine,
    *,
    project_id: uuid.UUID | None = None,
    now: datetime | None = None,
    actor: str = "orchestrator",
) -> BlockerSweepResult:
    """One scheduler tick (TITAN-WFL-019): detect blocked tasks and run the recovery
    ladder — dependency re-check → reassign → decompose → human escalation — one rung
    per still-blocked task. ``now`` is injected for a deterministic, clock-free test.
    A task that is no longer blocked has its open block recovered."""
    settings = get_settings()
    tick = now or datetime.now(UTC)
    async with engine.connect() as conn:
        rows = await repo.snapshots_for_sweep(conn, project_id)

    scanned = detected = advanced = recovered = escalated = 0
    for row in rows:
        scanned += 1
        snap = blockers.TaskSnapshot(
            task_id=str(row.task_id),
            status=row.status,
            estimate_points=row.estimate_points,
            updated_at=row.updated_at,
            dependency_failed=row.dependency_failed,
            awaiting_clarification=row.awaiting_clarification,
            loop_escalation=row.loop_escalation,
        )
        try:
            async with engine.begin() as conn:
                active = await repo.get_active_block(conn, row.task_id)
                if active is not None:
                    # A block already exists: recover if its root cause cleared, else
                    # advance one rung. Uses condition_persists (not detect_block) so the
                    # ladder's own writes cannot spuriously reset a stall clock.
                    if not blockers.condition_persists(snap, active.reason):
                        await repo.update_block(
                            conn,
                            active.id,
                            reason=active.reason,
                            rung=active.rung,
                            status="recovered",
                            detail="no longer blocked",
                            resolve=True,
                        )
                        await _emit_blocker(
                            conn, row, active.reason, active.rung, "recovered", actor
                        )
                        recovered += 1
                    elif active.status == "escalated":
                        pass  # terminal — awaiting a human, do not re-process
                    else:
                        new_rung = blockers.next_rung(active.rung)
                        status = "open"
                        if new_rung == blockers.RUNG_REASSIGN:
                            await repo.clear_assignee(conn, row.task_id)  # to a fresh instance
                        elif new_rung == blockers.RUNG_HUMAN:
                            status = "escalated"
                        # The decompose rung emits its blocker event (below) as the
                        # signal a decomposer agent consumes to split the task into
                        # subtasks; materialising the split is the agent's job, not the
                        # deterministic sweep's.
                        await repo.update_block(
                            conn,
                            active.id,
                            reason=active.reason,
                            rung=new_rung,
                            status=status,
                            detail=f"recovery rung: {new_rung}",
                            resolve=False,
                        )
                        await _emit_blocker(conn, row, active.reason, new_rung, status, actor)
                        if status == "escalated":
                            escalated += 1
                        else:
                            advanced += 1
                    continue
                # No active block yet: detect, and on a hit record the block + perform
                # the first ladder rung (dependency re-check) in the same tick.
                reason = blockers.detect_block(
                    snap,
                    tick,
                    stall_multiplier=settings.blocker_stall_multiplier,
                    hours_per_point=settings.blocker_hours_per_point,
                    default_points=settings.blocker_default_points,
                )
                if reason is not None:
                    await repo.insert_block(
                        conn,
                        row.project_id,
                        row.task_id,
                        reason=reason,
                        rung=blockers.RUNG_DEPENDENCY_RECHECK,
                        detail=f"blocked: {reason}",
                    )
                    await _emit_blocker(
                        conn, row, reason, blockers.RUNG_DEPENDENCY_RECHECK, "open", actor
                    )
                    detected += 1
        except IntegrityError:
            # A concurrent sweep inserted this task's block first (the partial-unique
            # guard fired); it is handled — skip to the next task.
            continue
    return BlockerSweepResult(
        scanned=scanned,
        detected=detected,
        advanced=advanced,
        recovered=recovered,
        escalated=escalated,
    )


async def _emit_blocker(
    conn: AsyncConnection, row: Row[Any], reason: str, rung: str, status: str, actor: str
) -> None:
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {
            "type": BLOCKER_EVENT,
            "blocker": {
                "project_id": str(row.project_id),
                "task_id": str(row.task_id),
                "reason": reason,
                "rung": rung,
                "status": status,
            },
        },
        actor=actor,
    )


__all__ = [
    "BlockerSweepResult",
    "CloseSprintIn",
    "CloseSprintResult",
    "EstimateActualIn",
    "MilestoneSignalsIn",
    "MilestoneStateOut",
    "MilestoneSyncResult",
    "OpenSprintIn",
    "PlanEdgeIn",
    "PlanRequest",
    "PlanTaskIn",
    "SprintCeremonyOut",
    "SprintPlanOut",
    "TaskBlockOut",
    "build_plan",
    "close_sprint",
    "compute_signals",
    "get_milestone",
    "latest_correction_factor",
    "list_blocks",
    "list_ceremonies",
    "open_sprint",
    "sweep_blockers",
    "sync_milestone",
]
