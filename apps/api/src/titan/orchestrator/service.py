"""Public service interface of the `orchestrator` module (TITAN-BCK-004).

Responsibility: task state machine, assignment, prioritization, scheduling.
Owns: tasks, task_dependencies.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.agents import service as agents
from titan.db import uuid7
from titan.events.service import STREAM_EVENTS_DOMAIN, STREAM_TASKS_READY, write_event
from titan.gateway.dto import RejectNullFields
from titan.gateway.pagination import Keyset, Page
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import not_found
from titan.messaging import service as messaging
from titan.orchestrator import locks, transitions
from titan.orchestrator import repository as repo
from titan.orchestrator.errors import DependencyCycleError, TransitionConflict, invalid_transition
from titan.orchestrator.transitions import Transition
from titan.runs import service as runs

# Canonical §8.1 board column order (backlog → cancelled).
TASK_STATUSES: tuple[str, ...] = (
    "backlog",
    "ready",
    "assigned",
    "in_progress",
    "in_review",
    "changes_requested",
    "done",
    "reopened",
    "cancelled",
)

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
    assignee_agent_id: uuid.UUID | None
    sprint_id: uuid.UUID | None


class BoardColumn(BaseModel):
    status: str
    tasks: list[BoardCard]


class Board(BaseModel):
    project_id: uuid.UUID
    columns: list[BoardColumn]


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


async def apply_transition(
    conn: AsyncConnection,
    task: TaskOut,
    transition: Transition,
    *,
    actor_agent_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    comment: str | None,
) -> TaskOut:
    """Execute one resolved transition atomically: guarded status move + the
    per-action side effects (assignee, review distinctness, reopen run,
    dependent promotion) + a messages row + the task.{action} outbox event.

    The caller (router or scheduler) has already resolved ``transition`` and
    checked ``required_permission``. Raises :class:`TransitionConflict` if the
    task left ``transition.from_status`` under us (the guard moved 0 rows)."""
    action = transition.action
    columns: dict[str, object] = {}

    # Serialize dependency-sensitive transitions on the global graph lock so
    # concurrent completions can't write-skew (two sibling blockers both missing
    # their shared dependent's promotion under READ COMMITTED).
    if action in ("ready", "approve"):
        await repo.acquire_dependency_graph_lock(conn)

    if action == "ready" and not await repo.dependencies_all_done(conn, task.id):
        raise _conflict("Cannot mark ready: not all dependencies are done.")
    if action == "assign":
        if actor_agent_id is None:
            raise _conflict("assign requires actor_agent_id (the agent to assign).")
        await _require_actor_in_project(conn, actor_agent_id, task.project_id)
        columns["assignee_agent_id"] = actor_agent_id
    if action in ("approve", "request_changes"):
        if actor_agent_id is None:
            raise _conflict("A review action requires a reviewer agent (actor_agent_id).")
        await _require_actor_in_project(conn, actor_agent_id, task.project_id)
        # TITAN-BCK-016: the reviewer MUST differ from the assignee — a task can
        # never reach done on a self-review.
        if task.assignee_agent_id is not None and actor_agent_id == task.assignee_agent_id:
            raise _conflict("The reviewer must be different from the assignee.")
        columns["reviewer_agent_id"] = actor_agent_id

    row = await repo.transition_status(
        conn,
        task.id,
        from_status=transition.from_status,
        to_status=transition.to_status,
        columns=columns,
        set_completed=(action == "approve"),
        clear_completed=(action == "reopen"),  # reopened is no longer 'done'
    )
    if row is None:
        raise TransitionConflict(transition.from_status, action)
    updated = TaskOut.model_validate(row)

    if action == "reopen" and updated.assignee_agent_id is not None:
        # TITAN-BCK-016: reopen creates a fresh run attempt (max+1) for retry.
        await runs.create_run(conn, updated.id, runs.RunCreate(agent_id=updated.assignee_agent_id))
    if action == "approve":
        await _promote_ready_dependents(conn, updated.id)

    await _write_transition_message(
        conn,
        updated,
        transition,
        actor_agent_id=actor_agent_id,
        actor_user_id=actor_user_id,
        comment=comment,
    )
    await _emit(conn, f"task.{action}", updated, str(actor_agent_id or actor_user_id or "system"))
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
    "TaskCreate",
    "TaskOut",
    "TaskPatch",
    "Transition",
    "TransitionConflict",
    "TransitionRequest",
    "add_dependency",
    "add_task_dependency",
    "apply_transition",
    "board_for_project",
    "claim_and_assign",
    "create_task",
    "get_task",
    "graph_for_project",
    "invalid_transition",
    "list_tasks",
    "ready_pool",
    "task_in_project",
    "transitions",
    "update_task",
]
