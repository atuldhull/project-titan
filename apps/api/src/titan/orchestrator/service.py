"""Public service interface of the `orchestrator` module (TITAN-BCK-004).

Responsibility: task state machine, assignment, prioritization, scheduling.
Owns: tasks, task_dependencies.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field
from redis.asyncio import Redis
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.agents import service as agents
from titan.db import uuid7
from titan.events.service import STREAM_EVENTS_DOMAIN, STREAM_TASKS_READY, write_event
from titan.gateway.dto import RejectNullFields
from titan.gateway.pagination import Keyset, Page
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import not_found
from titan.llm import service as llm
from titan.memory import service as memory
from titan.messaging import service as messaging
from titan.orchestrator import locks, pipeline, transitions
from titan.orchestrator import repository as repo
from titan.orchestrator.errors import DependencyCycleError, TransitionConflict, invalid_transition
from titan.orchestrator.transitions import Transition
from titan.runs import service as runs

# Canonical board column order: the §8.1 engineering states followed by the Vol 6
# §2.5 delivery states added in Phase 41 (one machine — see transitions.STATES).
TASK_STATUSES: tuple[str, ...] = transitions.STATES

# Fibonacci estimate scale (TITAN-BCK-013): grooming sets 1,2,3,5,8,13.
EstimatePoints = Literal[1, 2, 3, 5, 8, 13]


# --- DTOs ------------------------------------------------------------------


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    spec: str = ""
    priority: int = Field(default=3, ge=1, le=5)
    estimate_points: EstimatePoints | None = None


class TaskPatch(RejectNullFields):
    # Deliberately NO `status` field: state moves only through the future
    # :transition endpoint (§4.2 / §8.1). extra="forbid" turns a PATCH that
    # smuggles a `status` (or any unknown field) into a 422 problem — the
    # phase's hard requirement that PATCH must not change task state.
    # title/spec/priority map to NOT NULL columns → reject explicit null (422);
    # estimate_points is nullable (clearable).
    model_config = ConfigDict(extra="forbid")
    non_nullable = frozenset({"title", "spec", "priority"})

    title: str | None = Field(default=None, min_length=1, max_length=500)
    spec: str | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    estimate_points: EstimatePoints | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    sprint_id: uuid.UUID | None
    milestone_id: uuid.UUID | None
    parent_id: uuid.UUID | None
    title: str
    spec: str
    status: str
    priority: int
    estimate_points: int | None
    assignee_agent_id: uuid.UUID | None
    reviewer_agent_id: uuid.UUID | None
    created_by_agent_id: uuid.UUID | None
    completed_at: datetime | None
    created_at: datetime
    version: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def wfl_lane(self) -> str:
        """The Vol 6 §2.5 Kanban lane for this task's status (TITAN-WFL-009) — a
        pure projection of the physical status onto the delivery vocabulary."""
        return transitions.wfl_lane(self.status)


class DependencyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depends_on_id: uuid.UUID


class DependencyOut(BaseModel):
    task_id: uuid.UUID
    depends_on_id: uuid.UUID


class BoardCard(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    status: str
    priority: int
    #: Story points (§6.3 renders them on the card). Always been a `tasks` column;
    #: it was simply never projected onto the board.
    estimate_points: int | None
    assignee_agent_id: uuid.UUID | None
    sprint_id: uuid.UUID | None


class BoardColumn(BaseModel):
    status: str
    tasks: list[BoardCard]


class Board(BaseModel):
    project_id: uuid.UUID
    columns: list[BoardColumn]


# --- the task workflow definition (Vol 4 §6.3) ------------------------------


class WorkflowTransition(BaseModel):
    """One legal edge of the task state machine, as published to clients."""

    action: str
    from_status: str
    to_status: str
    #: The RBAC permission the caller needs — a board gates the drag on this.
    required_permission: str
    #: True when the action is meaningless without naming an agent (`assign` names
    #: the assignee; the review actions name the reviewer).
    requires_actor_agent: bool


class WorkflowDefinition(BaseModel):
    """The task state machine (TITAN-BCK-012), served as data.

    Vol 4 §6.3 wants a Kanban whose columns are "the workflow states" and whose
    drags are legal moves. Both are THIS table, which until now lived only inside
    the API: a client either hardcoded a copy (guaranteed to drift from
    `transitions.py`) or discovered the rules from 409s. Publishing it is the
    honest fix — one source of truth, read by both.

    `states` is the canonical column order. `transitions` is exhaustive, so a
    (from_status, to_status) pair absent from it is a move the board must refuse
    to offer. It carries NO per-project configuration and no WIP limits: the
    machine is global, and neither Vol 6 nor the backend defines either concept.

    Legality here is necessary, not sufficient — actions additionally carry
    server-side preconditions (`ready` needs its dependencies done, `complete`
    needs a reflection on record, done-entry needs no unflushed working memory
    and no Security veto). Those stay server-side and surface as a 409 with a
    human-readable detail; a client cannot pre-compute them.
    """

    states: list[str]
    transitions: list[WorkflowTransition]


class GraphNode(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    status: str


class GraphEdge(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: uuid.UUID
    depends_on_id: uuid.UUID


class Graph(BaseModel):
    project_id: uuid.UUID
    nodes: list[GraphNode]
    edges: list[GraphEdge]


async def _emit(conn: AsyncConnection, event_type: str, task: TaskOut, actor: str) -> None:
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {"type": event_type, "task": task.model_dump(mode="json")},
        actor=actor,
    )


async def _emit_kanban(
    conn: AsyncConnection,
    task: TaskOut,
    *,
    from_status: str,
    to_status: str,
    action: str,
    actor: str,
    actor_agent_id: uuid.UUID | None = None,
) -> None:
    """Emit the Vol 6 §2.5 Kanban event every task state change SHALL produce
    (TITAN-WFL-009). One outbox row on the domain stream; the Phase-24 fan-out
    republishes it to ``ws.project.{id}`` as a ``task.kanban`` frame so the Kanban
    artifact updates live. Carries the §2.5 lane so the board never has to know
    the fine-grained engineering statuses.

    ``actor_agent_id`` is the agent this transition NAMES — the request's
    ``actor_agent_id`` verbatim, which is also what the row records as its actor.
    Be precise about what that does and does not mean:

    * For most actions an agent names ITSELF, so it is the mover.
    * For ``assign``/``approve``/``request_changes`` it is the agent the action
      names — the assignee or the reviewer — and a HUMAN may have named it. So it
      is not reliably "who moved this", and a client must not say so.
    * It is None only when NO agent was named (a human or system move with no
      actor_agent_id), which is what lets a client tell a move that involves an
      agent from a purely human one.

    The API cannot do better here: the router sets ``actor_user_id`` to None the
    moment a caller supplies ``actor_agent_id``, so by this point "a human named
    an agent" and "an agent acted" are already indistinguishable.

    It is still worth publishing: the outbox row's ``actor`` is an untyped id
    string a subscriber cannot tell apart from a user's, and this is typed."""
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {
            "type": "task.kanban",
            "kanban": {
                "task_id": str(task.id),
                "project_id": str(task.project_id),
                "title": task.title,
                "status": task.status,
                "lane": transitions.wfl_lane(to_status),
                "from_lane": transitions.wfl_lane(from_status),
                "action": action,
                "priority": task.priority,
                "assignee_agent_id": (
                    str(task.assignee_agent_id) if task.assignee_agent_id is not None else None
                ),
                "actor_agent_id": (str(actor_agent_id) if actor_agent_id is not None else None),
            },
        },
        actor=actor,
    )


# --- tasks -----------------------------------------------------------------


async def create_task(
    conn: AsyncConnection, project_id: uuid.UUID, data: TaskCreate, actor: str
) -> TaskOut:
    row = await repo.insert_task(
        conn,
        task_id=uuid7(),
        project_id=project_id,
        title=data.title,
        spec=data.spec,
        priority=data.priority,
        estimate_points=data.estimate_points,
    )
    task = TaskOut.model_validate(row)
    await _emit(conn, "task.created", task, actor)
    return task


async def get_task(conn: AsyncConnection, task_id: uuid.UUID) -> TaskOut:
    row = await repo.get_task(conn, task_id)
    if row is None:
        raise not_found("task")
    return TaskOut.model_validate(row)


async def list_tasks(
    conn: AsyncConnection, *, project_id: uuid.UUID, keyset: Keyset, status: str | None
) -> Page[TaskOut]:
    rows = await repo.list_tasks_by_project(
        conn, project_id=project_id, keyset=keyset, status=status
    )
    page, cursor = keyset.paginate(rows)
    return Page(items=[TaskOut.model_validate(r) for r in page], next_cursor=cursor)


async def update_task(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    patch: TaskPatch,
    expected_version: int,
    actor: str,
) -> TaskOut | None:
    """Version-guarded partial update; None if the optimistic lock lost (412)."""
    fields = patch.model_dump(exclude_unset=True)
    row = await repo.update_task(conn, task_id, expected_version, fields)
    if row is None:
        return None
    task = TaskOut.model_validate(row)
    await _emit(conn, "task.updated", task, actor)
    return task


async def task_in_project(conn: AsyncConnection, task_id: uuid.UUID, project_id: uuid.UUID) -> bool:
    """True if the task exists AND belongs to ``project_id`` — the cross-module
    referential check callers use before linking to a task (e.g. a message's
    task_id must live in the message's project)."""
    row = await repo.get_task(conn, task_id)
    return row is not None and row.project_id == project_id


async def add_dependency(
    conn: AsyncConnection, *, task_id: uuid.UUID, depends_on_id: uuid.UUID, actor: str
) -> DependencyOut:
    """Add a ``task_id → depends_on_id`` edge, rejecting cycles with 409
    (problem type invalid-dependency-cycle). Reuses the Phase-6 recursive-CTE
    check — there is only one cycle detector."""
    if not await repo.task_exists(conn, depends_on_id):
        raise not_found("task")
    # Raises DependencyCycleError (→ 409) on a self-edge or a closed cycle.
    await add_task_dependency(conn, task_id, depends_on_id)
    touched = await repo.bump_task_version(conn, task_id)
    if touched is None:
        raise not_found("task")
    await _emit(conn, "task.updated", TaskOut.model_validate(touched), actor)
    return DependencyOut(task_id=task_id, depends_on_id=depends_on_id)


# --- task state machine (TITAN-BCK-012/014/015/016) ------------------------


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Free-form: an action illegal from the current status is a 409, not a 422.
    action: str = Field(min_length=1, max_length=50)
    #: The agent performing the transition (required for assign and review
    #: actions; the reviewer for approve/request_changes).
    actor_agent_id: uuid.UUID | None = None
    comment: str | None = Field(default=None, max_length=5000)


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type=problem_type("conflict")
    )


async def _require_actor_in_project(
    conn: AsyncConnection, agent_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """A transition's actor_agent_id (the assignee/reviewer) must be a real agent
    in the task's project — else the FK write would 500. A bad id is a 422."""
    if not await agents.agent_in_project(conn, agent_id, project_id):
        raise ProblemException(
            status=422,
            title="Invalid transition",
            detail="actor_agent_id is not an agent in this project.",
            problem_type=problem_type("validation"),
        )


async def _enqueue_ready(conn: AsyncConnection, task: TaskOut) -> None:
    """Publish a newly-ready task to stream:tasks.ready THROUGH the outbox (never
    a raw XADD) so a worker picks it up within ~1 s (TITAN-BCK-014)."""
    await write_event(
        conn,
        STREAM_TASKS_READY,
        {
            "type": "task.ready",
            "task_id": str(task.id),
            "project_id": str(task.project_id),
            "priority": task.priority,
        },
        actor="scheduler",
    )


async def _promote_ready_dependents(conn: AsyncConnection, completed_task_id: uuid.UUID) -> None:
    """When a task reaches done, move each backlog dependent whose deps are now
    ALL done to ready — in the SAME transaction — and enqueue it
    (TITAN-BCK-014). One level: a dependent's own dependents stay blocked until
    it too completes."""
    for dep_id in await repo.backlog_dependents(conn, completed_task_id):
        if not await repo.dependencies_all_done(conn, dep_id):
            continue
        row = await repo.transition_status(conn, dep_id, from_status="backlog", to_status="ready")
        if row is None:
            continue
        dep = TaskOut.model_validate(row)
        await _emit(conn, "task.ready", dep, "scheduler")
        await _emit_kanban(
            conn, dep, from_status="backlog", to_status="ready", action="ready", actor="scheduler"
        )
        await _enqueue_ready(conn, dep)


async def _write_transition_message(
    conn: AsyncConnection,
    task: TaskOut,
    transition: Transition,
    *,
    actor_agent_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    comment: str | None,
) -> None:
    """One messages row per actor-driven transition (kind matches the action,
    TITAN-BCK-012) — this is the review-approval record BCK-016 requires."""
    if transition.message_kind is None:  # system transition (scheduler ready)
        return
    body: dict[str, object] = {
        "action": transition.action,
        "from": transition.from_status,
        "to": transition.to_status,
    }
    if comment is not None:
        body["comment"] = comment
    await messaging.create_message(
        conn,
        task.project_id,
        messaging.MessageCreate(kind=transition.message_kind, body=body, task_id=task.id),
        sender_agent_id=actor_agent_id,
        sender_user_id=None if actor_agent_id is not None else actor_user_id,
        actor=str(actor_agent_id or actor_user_id or "system"),
    )


#: The 409 detail for an actor-requiring action invoked without an agent. Keyed
#: by action so each says what the agent is FOR; the requirement itself is the
#: table's `requires_actor_agent`, never this map's membership.
_ACTOR_REQUIRED_DETAIL: dict[str, str] = {
    "assign": "assign requires actor_agent_id (the agent to assign).",
    "approve": "A review action requires a reviewer agent (actor_agent_id).",
    "request_changes": "A review action requires a reviewer agent (actor_agent_id).",
}


async def apply_transition(
    conn: AsyncConnection,
    task: TaskOut,
    transition: Transition,
    *,
    actor_agent_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    comment: str | None,
    redis: Redis | None = None,
) -> TaskOut:
    """Execute one resolved transition atomically: guarded status move + the
    per-action side effects (assignee, review distinctness, reopen run,
    dependent promotion) + a messages row + the task.{action} outbox event.

    The caller (router or scheduler) has already resolved ``transition`` and
    checked ``required_permission``. Raises :class:`TransitionConflict` if the
    task left ``transition.from_status`` under us (the guard moved 0 rows).

    ``redis`` is REQUIRED for ``approve`` (the move into ``done``): the
    TITAN-BRN-002 gate must consult ``wm:{task_id}:events`` — completing a
    task while Memory Events sit unflushed in working memory would make Redis
    the sole copy of something durable. Approve with ``redis=None`` fails
    LOUDLY (config error), never silently skips the gate; the non-approve
    internal callers (assign/start/submit_review) never consult it."""
    action = transition.action
    columns: dict[str, object] = {}
    # Both done-entry actions: the fine-grained engineering `approve` (in_review →
    # done) and the Vol 6 §2.5 delivery `complete` (merged → done). They share the
    # done-entry side effects (BRN-002 gate, merge-veto, completed_at, dependent
    # promotion); `complete` additionally enforces the §2.5 acceptance gate below.
    is_done_entry = action in ("approve", "complete")

    if is_done_entry:
        if redis is None:
            raise RuntimeError(
                f"{action} requires a redis client for the TITAN-BRN-002 "
                "pending-working-memory-events gate"
            )
        pending = await memory.pending_wm_events(redis, task.id)
        if pending:
            raise _conflict(
                f"Cannot complete: {pending} working-memory event(s) for this task "
                "are not yet flushed to durable memory (TITAN-BRN-002)."
            )

    # TITAN-WFL-009: `merged -> done` requires the parent story's acceptance
    # criteria to pass. The concrete evidence is a schema-valid task reflection —
    # the done-block hook (TITAN-BRN-025). The engineering `approve` path keeps its
    # own review gate and is unaffected.
    if action == "complete" and not await memory.task_reflection_exists(conn, task.id):
        raise _conflict(
            "Cannot complete: the task's acceptance criteria are not verified — "
            "no schema-valid reflection on record (TITAN-WFL-009 / TITAN-BRN-025)."
        )

    # TITAN-WFL-010: `in_loop -> merged` requires a converged self-improvement loop
    # record — no component ships as a first draft. Lazy import: loop.service imports
    # this module (to drive task transitions), so a top-level import would cycle.
    if action == "merge":
        from titan.loop import service as loop

        await loop.assert_loop_approved(conn, task.id)

    # Serialize dependency-sensitive transitions on the global graph lock so
    # concurrent completions can't write-skew (two sibling blockers both missing
    # their shared dependent's promotion under READ COMMITTED).
    if action == "ready" or is_done_entry:
        await repo.acquire_dependency_graph_lock(conn)

    if action == "ready" and not await repo.dependencies_all_done(conn, task.id):
        raise _conflict("Cannot mark ready: not all dependencies are done.")
    # The actor-agent requirement is READ from the transition table rather than
    # re-listed here: `requires_actor_agent` is exactly what /api/v1/workflow
    # publishes, so a client that pre-collects the agent and this 409 cannot
    # disagree about which actions need one.
    if transition.requires_actor_agent:
        if actor_agent_id is None:
            raise _conflict(
                _ACTOR_REQUIRED_DETAIL.get(action, f"{action} requires actor_agent_id.")
            )
        # Every actor-requiring action names an agent that must be in the project.
        await _require_actor_in_project(conn, actor_agent_id, task.project_id)
        if action == "assign":
            columns["assignee_agent_id"] = actor_agent_id
        elif action in ("approve", "request_changes"):
            # TITAN-BCK-016: the reviewer MUST differ from the assignee — a task can
            # never reach done on a self-review.
            if task.assignee_agent_id is not None and actor_agent_id == task.assignee_agent_id:
                raise _conflict("The reviewer must be different from the assignee.")
            columns["reviewer_agent_id"] = actor_agent_id
    if is_done_entry:
        # §7.4 / TITAN-AGT-018: reaching `done` is the merge. A Security veto
        # blocks it regardless of this (or any) approval — the gate consults ONLY
        # the veto table, so no approval path, not even a CEO release, bypasses it.
        # Lazy import: messaging.service (which conflict.service uses) imports this
        # module, so a top-level import here would close a cycle.
        from titan.conflict import service as conflict

        await conflict.assert_merge_allowed(conn, task.project_id, task_id=task.id)

    row = await repo.transition_status(
        conn,
        task.id,
        from_status=transition.from_status,
        to_status=transition.to_status,
        columns=columns,
        set_completed=is_done_entry,
        clear_completed=(action == "reopen"),  # reopened is no longer 'done'
    )
    if row is None:
        raise TransitionConflict(transition.from_status, action)
    updated = TaskOut.model_validate(row)

    if action == "reopen":
        # TITAN-WFL-010: reopening re-works the task, so a prior converged loop no
        # longer covers it — retire it (a fresh loop must converge before it can
        # merge again). Lazy import to avoid the loop↔orchestrator cycle.
        from titan.loop import service as loop

        await loop.supersede_loop_on_reopen(conn, updated.id)
        if updated.assignee_agent_id is not None:
            # TITAN-BCK-016: reopen creates a fresh run attempt (max+1) for retry.
            await runs.create_run(
                conn, updated.id, runs.RunCreate(agent_id=updated.assignee_agent_id)
            )
    if is_done_entry:
        await _promote_ready_dependents(conn, updated.id)

    await _write_transition_message(
        conn,
        updated,
        transition,
        actor_agent_id=actor_agent_id,
        actor_user_id=actor_user_id,
        comment=comment,
    )
    event_actor = str(actor_agent_id or actor_user_id or "system")
    await _emit(conn, f"task.{action}", updated, event_actor)
    await _emit_kanban(
        conn,
        updated,
        from_status=transition.from_status,
        to_status=updated.status,
        action=action,
        actor=event_actor,
        actor_agent_id=actor_agent_id,
    )
    if updated.status == "ready":
        await _enqueue_ready(conn, updated)
    return updated


async def ready_pool(conn: AsyncConnection, project_id: uuid.UUID) -> list[TaskOut]:
    """The scheduler's ordered `ready` pool (TITAN-BCK-013)."""
    return [TaskOut.model_validate(r) for r in await repo.ready_pool_rows(conn, project_id)]


async def claim_and_assign(
    engine: AsyncEngine, redis: Redis, task_id: uuid.UUID, agent_id: uuid.UUID
) -> TaskOut | None:
    """Race-safe scheduler assignment (TITAN-BCK-015): take lock:task:{id}
    (SET NX PX), then move ready -> assigned. Returns the assigned task, or None
    if another worker holds the lock OR the task is no longer ready. Two workers
    racing one task yield exactly one winner (the DB guard is the final arbiter;
    the lock just avoids wasted work)."""
    token = await locks.acquire(redis, task_id)
    if token is None:
        return None
    try:
        async with engine.begin() as conn:
            task = await get_task(conn, task_id)
            # Budget hold PAUSES the task (§ point 4): at 100% budget no new work
            # is assigned and no LLM call is made for it. The task keeps its status
            # (still `ready`), so raising the budget resumes it exactly here.
            held = await llm.active_budget_hold(conn, project_id=task.project_id, task_id=task.id)
            if held is not None:
                return None
            transition = transitions.resolve(task.status, "assign")
            if transition is None:
                return None  # not in `ready`
            return await apply_transition(
                conn, task, transition, actor_agent_id=agent_id, actor_user_id=None, comment=None
            )
    except TransitionConflict:
        return None  # lost the race — another claimer moved it out of ready
    finally:
        await locks.release(redis, task_id, token)


# --- run-lifecycle hooks (called by the worker's run coordinator, §8.2) -----


async def start_run_task(
    conn: AsyncConnection, task_id: uuid.UUID, agent_id: uuid.UUID
) -> TaskOut | None:
    """The worker begins a run: move the task assigned|reopened -> in_progress.
    Idempotent — returns None if the task is not in a startable state (e.g. a
    reclaimed run whose task is already in_progress)."""
    task = await get_task(conn, task_id)
    transition = transitions.resolve(task.status, "start")
    if transition is None:
        return None
    return await apply_transition(
        conn, task, transition, actor_agent_id=agent_id, actor_user_id=None, comment=None
    )


async def handle_run_result(
    conn: AsyncConnection,
    *,
    task_id: uuid.UUID,
    agent_id: uuid.UUID,
    succeeded: bool,
) -> TaskOut | None:
    """Drive the state machine from a runs.results message (§8.2): a successful
    run submits the task for review (in_progress -> in_review). A failed run is
    terminal and leaves the task in_progress — recovery is a NEW dispatched run
    (the scheduler re-dispatches, or a human intervenes); it is NOT auto-retried
    here (only a timed_out run's queue message is retried, by the reaper).
    Idempotent — a duplicate result finds the task already moved and no-ops."""
    if not succeeded:
        return None
    task = await get_task(conn, task_id)
    transition = transitions.resolve(task.status, "submit_review")
    if transition is None:
        return None
    return await apply_transition(
        conn,
        task,
        transition,
        actor_agent_id=agent_id,
        actor_user_id=None,
        comment="run succeeded",
    )


# --- delivery pipeline state machine (Vol 6 §2.1, TITAN-WFL-001/002) --------

# The ordered 12 stages, re-exported so callers never reach into the private
# pipeline module.
PIPELINE_STAGES: tuple[str, ...] = pipeline.STAGES


class PipelineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_id: uuid.UUID
    current_stage: str
    iteration: int
    status: str
    retry_budget: int
    version: int
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def current_stage_label(self) -> str:
        """Human label for the current stage (dashboard) — a pure projection."""
        return pipeline.STAGE_LABELS.get(self.current_stage, self.current_stage)


class PipelineStageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    stage: str
    event: str
    iteration: int
    actor: str
    detail: dict[str, Any]
    at: datetime


class PipelineView(BaseModel):
    project_id: uuid.UUID
    pipeline: PipelineOut
    stages: list[str]
    history: list[PipelineStageEventOut]


class PipelineAdvanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The stage to advance to. Must be the immediate successor of the current
    #: stage (TITAN-WFL-002) — any other value is a rejected out-of-order attempt.
    target_stage: str = Field(min_length=1, max_length=50)
    note: str | None = Field(default=None, max_length=2000)


class PipelineNotApplicableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_stage: str = Field(min_length=1, max_length=50)
    #: The CEO's logged justification for skipping the current stage (§2.2/§2.5).
    justification: str = Field(min_length=1, max_length=2000)


class PipelineRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


def _pipeline_out(row: Row[Any]) -> PipelineOut:
    return PipelineOut.model_validate(row)


async def _record_stage_event(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    stage: str,
    event: str,
    iteration: int,
    actor: str,
    detail: dict[str, Any] | None = None,
) -> None:
    await repo.append_stage_event(
        conn,
        project_id,
        stage=stage,
        event=event,
        iteration=iteration,
        actor=actor,
        detail=json.dumps(detail or {}),
    )


async def _emit_pipeline(
    conn: AsyncConnection, out: PipelineOut, *, change: dict[str, Any], actor: str
) -> None:
    """Emit the pipeline event through the transactional outbox (Phase 10). The
    Phase-24 fan-out republishes it to ``ws.project.{id}`` as a ``pipeline.updated``
    frame — the per-project ``pipeline.<project>.stage_complete`` of §2.1, realized
    on the one domain stream + WS channel (no second transport)."""
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {
            "type": "pipeline.updated",
            "pipeline": out.model_dump(mode="json"),
            "change": change,
        },
        actor=actor,
    )


async def ensure_pipeline(
    conn: AsyncConnection, project_id: uuid.UUID, *, actor: str = "system"
) -> PipelineOut:
    """Idempotently initialize the project's pipeline at the first stage. On first
    creation it logs an ``enter`` ledger row and emits the pipeline event; a
    subsequent call is a pure read (the cursor already exists)."""
    existing = await repo.get_pipeline_state(conn, project_id)
    if existing is not None:
        return _pipeline_out(existing)
    row, inserted = await repo.ensure_pipeline_state(
        conn, project_id, initial_stage=pipeline.INITIAL_STAGE
    )
    out = _pipeline_out(row)
    # A concurrent initializer may have won the INSERT; ONLY the caller that
    # actually inserted logs the opening 'enter' event + emits it, so the
    # append-only ledger never carries a duplicate opening row (TITAN-WFL-001).
    if inserted:
        await _record_stage_event(
            conn,
            project_id,
            stage=out.current_stage,
            event="enter",
            iteration=out.iteration,
            actor=actor,
        )
        await _emit_pipeline(
            conn, out, change={"event": "enter", "stage": out.current_stage}, actor=actor
        )
    return out


async def ensure_and_lock_pipeline(
    conn: AsyncConnection, project_id: uuid.UUID, *, actor: str = "system"
) -> PipelineOut:
    """Ensure the pipeline exists, then ``SELECT … FOR UPDATE`` its row so the
    caller's whole transaction is serialized per project. The handoff executor
    holds this lock for its validate → store → gate → advance/retry/escalate unit,
    making the TITAN-WFL-005 retry budget + escalation exactly-once and closing the
    stage-skip / duplicate-version races that concurrent submissions would open
    under READ COMMITTED. Returns the freshest state under the lock."""
    await ensure_pipeline(conn, project_id, actor=actor)
    row = await repo.lock_pipeline_state(conn, project_id)
    assert row is not None  # ensure_pipeline just created-or-confirmed the row
    return _pipeline_out(row)


async def get_pipeline(conn: AsyncConnection, project_id: uuid.UUID) -> PipelineView:
    """The project's pipeline cursor + full ordered stage history (ledger replay).
    Auto-initializes on first read so every project has a pipeline."""
    out = await ensure_pipeline(conn, project_id)
    history = [
        PipelineStageEventOut.model_validate(r) for r in await repo.stage_events(conn, project_id)
    ]
    return PipelineView(
        project_id=project_id, pipeline=out, stages=list(pipeline.STAGES), history=history
    )


async def _reject_pipeline(
    conn: AsyncConnection,
    out: PipelineOut,
    *,
    target_stage: str,
    actor: str,
    reason: str,
) -> ProblemException:
    """Log a rejected out-of-order transition (TITAN-WFL-002) and return the 409 to
    raise — state is left unchanged. The ledger + emitted event make the rejection
    auditable and observable on the dashboard."""
    await _record_stage_event(
        conn,
        out.project_id,
        stage=out.current_stage,
        event="rejected",
        iteration=out.iteration,
        actor=actor,
        detail={"target_stage": target_stage, "reason": reason},
    )
    await _emit_pipeline(
        conn,
        out,
        change={
            "event": "rejected",
            "stage": out.current_stage,
            "target_stage": target_stage,
            "reason": reason,
        },
        actor=actor,
    )
    return _conflict(reason)


async def _advance_or_complete(
    engine: AsyncEngine,
    project_id: uuid.UUID,
    *,
    target_stage: str,
    actor: str,
    completion_event: str,
    detail: dict[str, Any],
) -> PipelineOut:
    """Shared body for advance / not-applicable: enforce no-skip + budget hold,
    then close the current stage with ``completion_event`` (stage_complete or
    not_applicable) and enter the successor. Terminal successor → released.

    Owns its OWN transaction so a REJECTED / PAUSED outcome still COMMITS its
    ledger row and event (TITAN-WFL-002 requires the rejection to be logged) and
    THEN raises the 409 — raising inside the caller's transaction would roll the
    audit trail back with it. `not running` / raced outcomes have nothing to
    persist, so they raise inside and roll back."""
    pending: ProblemException | None = None
    result: PipelineOut | None = None
    async with engine.begin() as conn:
        out = await ensure_pipeline(conn, project_id)
        if out.status != "running":
            raise _conflict(
                f"Pipeline is {out.status}, not running — no stage transition is possible."
            )

        # Budget hold PAUSES the pipeline: no stage advance spends LLM at 100%.
        held = await llm.active_budget_hold(conn, project_id=project_id, task_id=None)
        if held is not None:
            await _record_stage_event(
                conn,
                project_id,
                stage=out.current_stage,
                event="budget_hold",
                iteration=out.iteration,
                actor=actor,
                detail={"reason": held},
            )
            await _emit_pipeline(
                conn,
                out,
                change={"event": "budget_hold", "stage": out.current_stage, "reason": held},
                actor=actor,
            )
            pending = _conflict(
                f"Pipeline paused on budget hold: {held}. Raise the budget to resume."
            )
        elif pipeline.resolve_advance(out.current_stage, target_stage) is None:
            pending = await _reject_pipeline(
                conn,
                out,
                target_stage=target_stage,
                actor=actor,
                reason=(
                    f"Illegal pipeline transition {out.current_stage} -> {target_stage}: stages "
                    "may not be skipped or reordered (TITAN-WFL-002); the only legal target is "
                    f"{pipeline.next_stage(out.current_stage)}."
                ),
            )
        else:
            result = await _do_advance(
                conn,
                project_id,
                out=out,
                target_stage=target_stage,
                completion_event=completion_event,
                detail=detail,
                actor=actor,
            )
    if pending is not None:
        raise pending
    assert result is not None  # exactly one of pending / result is set on the happy path
    return result


async def _do_advance(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    out: PipelineOut,
    target_stage: str,
    completion_event: str,
    detail: dict[str, Any],
    actor: str,
) -> PipelineOut:
    """Apply a resolved (legal, non-paused) advance: guarded stage move + the
    close-old / enter-new ledger rows + the released terminal + the event."""
    from_stage = out.current_stage
    from_iteration = out.iteration
    row = await repo.advance_pipeline_stage(
        conn, project_id, from_stage=from_stage, to_stage=target_stage
    )
    if row is None:
        # Another mover advanced this pipeline out of from_stage under us.
        raise _conflict(
            f"Pipeline is no longer at stage {from_stage}; the transition was applied concurrently."
        )
    await _record_stage_event(
        conn,
        project_id,
        stage=from_stage,
        event=completion_event,
        iteration=from_iteration,
        actor=actor,
        detail=detail,
    )
    advanced = _pipeline_out(row)
    await _record_stage_event(
        conn,
        project_id,
        stage=advanced.current_stage,
        event="enter",
        iteration=advanced.iteration,
        actor=actor,
    )
    change: dict[str, Any] = {
        "event": completion_event,
        "stage": from_stage,
        "next_stage": advanced.current_stage,
        **detail,
    }
    if pipeline.is_terminal(advanced.current_stage):
        released = await repo.set_pipeline_status(conn, project_id, status="released")
        if released is not None:
            advanced = _pipeline_out(released)
        await _record_stage_event(
            conn,
            project_id,
            stage=advanced.current_stage,
            event="released",
            iteration=advanced.iteration,
            actor=actor,
        )
        change["released"] = True
    await _emit_pipeline(conn, advanced, change=change, actor=actor)
    return advanced


async def advance_pipeline(
    engine: AsyncEngine,
    project_id: uuid.UUID,
    data: PipelineAdvanceRequest,
    *,
    actor: str,
) -> PipelineOut:
    """Complete the current stage and enter its immediate successor
    (TITAN-WFL-001). An out-of-order ``target_stage`` is rejected (409) with a
    logged rejected-transition event and no state change (TITAN-WFL-002).

    Takes the engine (not a connection) so a rejected/paused outcome commits its
    audit row before the 409 propagates — see :func:`_advance_or_complete`."""
    detail: dict[str, Any] = {}
    if data.note is not None:
        detail["note"] = data.note
    return await _advance_or_complete(
        engine,
        project_id,
        target_stage=data.target_stage,
        actor=actor,
        completion_event="stage_complete",
        detail=detail,
    )


async def mark_stage_not_applicable(
    engine: AsyncEngine,
    project_id: uuid.UUID,
    data: PipelineNotApplicableRequest,
    *,
    actor: str,
) -> PipelineOut:
    """The single sanctioned skip (TITAN-WFL-002): the CEO Agent marks the current
    stage ``NOT_APPLICABLE`` with a logged justification (e.g. a docs-only change
    skipping DB engineering), which still advances only to the immediate
    successor — never reorders or jumps. CEO authority is enforced at the route
    (``pipeline:override``)."""
    return await _advance_or_complete(
        engine,
        project_id,
        target_stage=data.target_stage,
        actor=actor,
        completion_event="not_applicable",
        detail={"justification": data.justification},
    )


async def retry_pipeline_stage(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    data: PipelineRetryRequest,
    *,
    actor: str,
) -> PipelineOut:
    """Re-enter the current stage, bumping its iteration count — a gate failure /
    rework re-entry (§2.2 retry budget). The (stage, iteration) pair is what a
    restarted orchestrator resumes on, so the counter is persisted."""
    out = await ensure_pipeline(conn, project_id)
    if out.status != "running":
        raise _conflict(f"Pipeline is {out.status}, not running — cannot retry a stage.")
    row = await repo.bump_pipeline_iteration(conn, project_id, stage=out.current_stage)
    if row is None:
        raise _conflict("Pipeline stage moved concurrently; retry not applied.")
    out = _pipeline_out(row)
    detail: dict[str, Any] = {}
    if data.reason is not None:
        detail["reason"] = data.reason
    await _record_stage_event(
        conn,
        project_id,
        stage=out.current_stage,
        event="enter",
        iteration=out.iteration,
        actor=actor,
        detail={"retry": True, **detail},
    )
    await _emit_pipeline(
        conn,
        out,
        change={"event": "retry", "stage": out.current_stage, "iteration": out.iteration},
        actor=actor,
    )
    return out


async def resume_pipeline_budget(
    conn: AsyncConnection, project_id: uuid.UUID, *, actor: str, redis: Redis | None = None
) -> PipelineOut:
    """Raise the project budget (clear the project hold + reset its rolling-spend
    counter) so a pipeline paused on budget hold resumes exactly where it paused
    (§ point 4). Idempotent — clearing an absent hold is a no-op that still logs
    the resume intent."""
    out = await ensure_pipeline(conn, project_id)
    await llm.raise_budget(conn, scope="project", scope_id=project_id, redis=redis)
    await _record_stage_event(
        conn,
        project_id,
        stage=out.current_stage,
        event="resumed",
        iteration=out.iteration,
        actor=actor,
    )
    await _emit_pipeline(
        conn, out, change={"event": "resumed", "stage": out.current_stage}, actor=actor
    )
    return out


async def advance_stage(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    actor: str,
    detail: dict[str, Any] | None = None,
) -> PipelineOut:
    """Advance the pipeline from its current stage to the immediate successor —
    the handoff executor's on-gate-pass move (Phase 42, TITAN-WFL-001). Conn-based
    so the executor keeps the advance ATOMIC with its artifact + gate-ledger
    writes. The caller has already validated the artifact and passed the gate, so
    this only enforces running + non-terminal; the successor is always legal by
    construction (no skip is possible)."""
    out = await ensure_pipeline(conn, project_id)
    if out.status != "running":
        raise _conflict(f"Pipeline is {out.status}, not running — cannot advance the stage.")
    successor = pipeline.next_stage(out.current_stage)
    if successor is None:
        raise _conflict(
            f"Pipeline is at the terminal stage {out.current_stage}; nothing to advance."
        )
    return await _do_advance(
        conn,
        project_id,
        out=out,
        target_stage=successor,
        completion_event="stage_complete",
        detail=detail or {},
        actor=actor,
    )


async def escalate_pipeline(
    conn: AsyncConnection, project_id: uuid.UUID, *, actor: str, reason: str
) -> PipelineOut:
    """Pause the pipeline and mark the project NEEDS_HUMAN (TITAN-WFL-005): once a
    stage's gate retry budget is exhausted, no further stage work proceeds until a
    human intervenes. Logs a needs_human ledger row + emits the event. Idempotent
    (re-escalating an already-paused pipeline just re-affirms the status)."""
    out = await ensure_pipeline(conn, project_id)
    row = await repo.set_pipeline_status(conn, project_id, status="needs_human")
    if row is not None:
        out = _pipeline_out(row)
    await _record_stage_event(
        conn,
        project_id,
        stage=out.current_stage,
        event="needs_human",
        iteration=out.iteration,
        actor=actor,
        detail={"reason": reason},
    )
    await _emit_pipeline(
        conn, out, change={"event": "needs_human", "stage": out.current_stage}, actor=actor
    )
    return out


async def reconcile_pipelines(conn: AsyncConnection) -> int:
    """Seed a pipeline for every non-archived project that lacks one (idempotent);
    returns how many were seeded. A (re)started orchestrator runs this to pick up
    projects created while it was down — the resume of an IN-FLIGHT pipeline is
    otherwise structural (the current_stage/iteration cursor lives in PostgreSQL,
    so restarting reads it straight back)."""
    seeded = 0
    for project_id in await repo.projects_without_pipeline(conn):
        await ensure_pipeline(conn, project_id, actor="orchestrator")
        seeded += 1
    return seeded


async def resume_task_budget(
    conn: AsyncConnection, task_id: uuid.UUID, *, actor: str, redis: Redis | None = None
) -> TaskOut:
    """Raise a task's budget (clear its task-scoped hold + reset its rolling-spend
    counter) so a task paused on budget hold resumes exactly where it paused
    (§ point 4). Emits a Kanban event so the board reflects the resume; idempotent
    when no hold is present."""
    task = await get_task(conn, task_id)
    await llm.raise_budget(conn, scope="task", scope_id=task_id, redis=redis)
    await _emit_kanban(
        conn, task, from_status=task.status, to_status=task.status, action="resume", actor=actor
    )
    return task


# --- read-only project aggregates ------------------------------------------


async def board_for_project(conn: AsyncConnection, project_id: uuid.UUID) -> Board:
    """Kanban board: one column per §8.1 status (empty columns included) in
    canonical order, plus any unexpected status defensively appended."""
    by_status: dict[str, list[BoardCard]] = {s: [] for s in TASK_STATUSES}
    for row in await repo.board_rows(conn, project_id):
        card = BoardCard.model_validate(row)
        by_status.setdefault(card.status, []).append(card)
    columns = [BoardColumn(status=s, tasks=by_status[s]) for s in TASK_STATUSES]
    columns += [
        BoardColumn(status=s, tasks=cards)
        for s, cards in by_status.items()
        if s not in TASK_STATUSES
    ]
    return Board(project_id=project_id, columns=columns)


def workflow_definition() -> WorkflowDefinition:
    """The task state machine as data (see :class:`WorkflowDefinition`).

    Pure — a projection of ``transitions._TABLE``, no DB, no I/O. Sorted for a
    stable response: `states` keeps the canonical board order from ``STATES``
    (NOT alphabetical), transitions by (from_status, action)."""
    return WorkflowDefinition(
        states=list(transitions.STATES),
        transitions=[
            WorkflowTransition(
                action=t.action,
                from_status=t.from_status,
                to_status=t.to_status,
                required_permission=t.required_permission,
                requires_actor_agent=t.requires_actor_agent,
            )
            for t in sorted(
                transitions.TRANSITIONS.values(), key=lambda t: (t.from_status, t.action)
            )
        ],
    )


async def graph_for_project(conn: AsyncConnection, project_id: uuid.UUID) -> Graph:
    nodes = [GraphNode.model_validate(r) for r in await repo.graph_nodes(conn, project_id)]
    edges = [GraphEdge.model_validate(r) for r in await repo.graph_edges(conn, project_id)]
    return Graph(project_id=project_id, nodes=nodes, edges=edges)


# --- dependency graph primitive (Phase 6) ----------------------------------

# The dependency-graph advisory lock (repo.acquire_dependency_graph_lock)
# serializes ALL graph writes AND completion-driven promotions on one global
# key, so the recursive-CTE reachability check always sees every committed edge
# and concurrent completions can't write-skew — safe even at READ COMMITTED.

_INSERT_EDGE = text(
    """
    INSERT INTO task_dependencies (task_id, depends_on_id)
    VALUES (:task_id, :depends_on_id)
    ON CONFLICT (task_id, depends_on_id) DO NOTHING
    """
)

# Reachability over depends_on edges, starting from the new edge's target:
# if task_id is reachable from depends_on_id, the new edge closed a cycle.
_CYCLE_CHECK = text(
    """
    WITH RECURSIVE reach(node) AS (
        SELECT depends_on_id FROM task_dependencies WHERE task_id = :depends_on_id
        UNION
        SELECT td.depends_on_id
        FROM task_dependencies td
        JOIN reach r ON td.task_id = r.node
    )
    SELECT 1 FROM reach WHERE node = :task_id LIMIT 1
    """
)


async def add_task_dependency(
    conn: AsyncConnection, task_id: uuid.UUID, depends_on_id: uuid.UUID
) -> None:
    """Insert a dependency edge, rejecting cycles (TITAN-BCK-009).

    Graph writes serialize on a transaction-scoped advisory lock, so the
    recursive-CTE reachability check always sees every committed edge —
    safe against concurrent writers even at READ COMMITTED. A raised
    :class:`DependencyCycleError` rolls the edge back with the enclosing
    transaction (the lock releases with it, commit or rollback).
    """
    if task_id == depends_on_id:
        raise DependencyCycleError(task_id, depends_on_id)

    await repo.acquire_dependency_graph_lock(conn)
    await conn.execute(_INSERT_EDGE, {"task_id": task_id, "depends_on_id": depends_on_id})
    cycle = await conn.execute(_CYCLE_CHECK, {"task_id": task_id, "depends_on_id": depends_on_id})
    if cycle.scalar() is not None:
        raise DependencyCycleError(task_id, depends_on_id)


__all__ = [
    "PIPELINE_STAGES",
    "TASK_STATUSES",
    "Board",
    "BoardCard",
    "BoardColumn",
    "DependencyCreate",
    "DependencyCycleError",
    "DependencyOut",
    "Graph",
    "GraphEdge",
    "GraphNode",
    "PipelineAdvanceRequest",
    "PipelineNotApplicableRequest",
    "PipelineOut",
    "PipelineRetryRequest",
    "PipelineStageEventOut",
    "PipelineView",
    "TaskCreate",
    "TaskOut",
    "TaskPatch",
    "Transition",
    "TransitionConflict",
    "TransitionRequest",
    "add_dependency",
    "add_task_dependency",
    "advance_pipeline",
    "advance_stage",
    "apply_transition",
    "board_for_project",
    "claim_and_assign",
    "create_task",
    "ensure_and_lock_pipeline",
    "ensure_pipeline",
    "escalate_pipeline",
    "get_pipeline",
    "get_task",
    "graph_for_project",
    "handle_run_result",
    "invalid_transition",
    "list_tasks",
    "mark_stage_not_applicable",
    "pipeline",
    "ready_pool",
    "reconcile_pipelines",
    "resume_pipeline_budget",
    "resume_task_budget",
    "retry_pipeline_stage",
    "start_run_task",
    "task_in_project",
    "transitions",
    "update_task",
]
