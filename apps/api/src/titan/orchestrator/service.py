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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7
from titan.events.service import STREAM_EVENTS_DOMAIN, write_event
from titan.gateway.dto import RejectNullFields
from titan.gateway.pagination import Keyset, Page
from titan.gateway.rbac import not_found
from titan.orchestrator import repository as repo
from titan.orchestrator.errors import DependencyCycleError

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


async def task_in_project(
    conn: AsyncConnection, task_id: uuid.UUID, project_id: uuid.UUID
) -> bool:
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

# Transaction-scoped advisory lock serializing ALL dependency-graph writes.
# Without it, two READ COMMITTED transactions inserting A→B and B→A each see
# a snapshot without the other's edge and both commit — a persistent cycle.
# Graph writes are rare planning operations; a single global lock is cheap.
# (Global, not per-project: the schema permits cross-project edges.)
_DEP_GRAPH_LOCK_KEY = 0x7469_7461_6E64_6570  # "titandep"

_ACQUIRE_GRAPH_LOCK = text("SELECT pg_advisory_xact_lock(:key)")

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

    await conn.execute(_ACQUIRE_GRAPH_LOCK, {"key": _DEP_GRAPH_LOCK_KEY})
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
    "add_dependency",
    "add_task_dependency",
    "board_for_project",
    "create_task",
    "get_task",
    "graph_for_project",
    "list_tasks",
    "task_in_project",
    "update_task",
]
