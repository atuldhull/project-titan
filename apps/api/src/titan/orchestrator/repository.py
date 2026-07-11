"""Private data access for the `orchestrator` module (tasks, task_dependencies,
and the read-only project aggregates board/graph). SQLAlchemy Core over an
AsyncConnection; only ``service.py`` is importable across the module boundary.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.gateway.pagination import Keyset

_TASK_COLS = (
    "id, project_id, sprint_id, milestone_id, parent_id, title, spec, status, priority, "
    "estimate_points, assignee_agent_id, reviewer_agent_id, created_by_agent_id, "
    "completed_at, created_at, version"
)


async def insert_task(
    conn: AsyncConnection,
    *,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    title: str,
    spec: str,
    priority: int,
    estimate_points: int | None,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO tasks (id, project_id, title, spec, priority, estimate_points) "
            f"VALUES (:id, :project_id, :title, :spec, :priority, :estimate_points) "
            f"RETURNING {_TASK_COLS}"
        ),
        {
            "id": task_id,
            "project_id": project_id,
            "title": title,
            "spec": spec,
            "priority": priority,
            "estimate_points": estimate_points,
        },
    )
    return result.one()


async def get_task(conn: AsyncConnection, task_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_TASK_COLS} FROM tasks WHERE id = :id"),
        {"id": task_id},
    )
    return result.one_or_none()


async def task_exists(conn: AsyncConnection, task_id: uuid.UUID) -> bool:
    result = await conn.execute(text("SELECT 1 FROM tasks WHERE id = :id"), {"id": task_id})
    return result.scalar() is not None


async def list_tasks_by_project(
    conn: AsyncConnection, *, project_id: uuid.UUID, keyset: Keyset, status: str | None
) -> Sequence[Row[Any]]:
    clauses = ["project_id = :pid"]
    params: dict[str, Any] = {"pid": project_id, "limit": keyset.fetch}
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status
    seek, seek_params = keyset.where()
    if seek:
        clauses.append(seek)
        params.update(seek_params)
    result = await conn.execute(
        text(
            f"SELECT {_TASK_COLS} FROM tasks WHERE {' AND '.join(clauses)} "
            f"ORDER BY {keyset.order_by} LIMIT :limit"
        ),
        params,
    )
    return result.all()


async def update_task(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    expected_version: int,
    fields: dict[str, Any],
) -> Row[Any] | None:
    assignments = ", ".join(f"{col} = :{col}" for col in fields)
    prefix = f"{assignments}, " if assignments else ""
    result = await conn.execute(
        text(
            f"UPDATE tasks SET {prefix}version = version + 1 "
            f"WHERE id = :id AND version = :expected RETURNING {_TASK_COLS}"
        ),
        {**fields, "id": task_id, "expected": expected_version},
    )
    return result.one_or_none()


async def bump_task_version(conn: AsyncConnection, task_id: uuid.UUID) -> Row[Any] | None:
    """Touch a task (version + 1) without changing a field — used when an edge
    is added so the task's ETag reflects the graph change."""
    result = await conn.execute(
        text(f"UPDATE tasks SET version = version + 1 WHERE id = :id RETURNING {_TASK_COLS}"),
        {"id": task_id},
    )
    return result.one_or_none()


# --- state machine (TITAN-BCK-012/014/015) ---------------------------------

# Global advisory lock serializing dependency-graph reads+writes. Without it,
# two READ COMMITTED transactions can write-skew: insert opposite edges (a
# cycle), or complete two sibling blockers and BOTH miss promoting their shared
# dependent (each sees its own done + the other's still-committed non-done).
# Graph/completion writes are rare planning ops, so one global lock is cheap.
DEPENDENCY_GRAPH_LOCK_KEY = 0x7469_7461_6E64_6570  # "titandep"


async def acquire_dependency_graph_lock(conn: AsyncConnection) -> None:
    """Take the transaction-scoped dependency-graph lock (releases at commit)."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": DEPENDENCY_GRAPH_LOCK_KEY}
    )


async def transition_status(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    *,
    from_status: str,
    to_status: str,
    columns: dict[str, Any] | None = None,
    set_completed: bool = False,
    clear_completed: bool = False,
) -> Row[Any] | None:
    """Guarded status move: applies ONLY while the row is still in
    ``from_status`` (``WHERE status = :from``). Returns None if the row is
    missing or has already left ``from_status`` — which makes every transition
    race-safe: two concurrent movers from one state, exactly one wins. ``columns``
    are extra assignments (e.g. assignee_agent_id); ``set_completed`` stamps
    completed_at = now(); ``clear_completed`` nulls it (reopen leaves 'done')."""
    columns = columns or {}
    sets = ["status = :__to", "version = version + 1"]
    sets += [f"{col} = :{col}" for col in columns]
    if set_completed:
        sets.append("completed_at = now()")
    elif clear_completed:
        sets.append("completed_at = NULL")
    result = await conn.execute(
        text(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = :__id AND status = :__from RETURNING {_TASK_COLS}"
        ),
        {**columns, "__id": task_id, "__to": to_status, "__from": from_status},
    )
    return result.one_or_none()


async def dependencies_all_done(conn: AsyncConnection, task_id: uuid.UUID) -> bool:
    """True if every task ``task_id`` depends on is done (so it may become ready).
    Vacuously true for a task with no dependencies (TITAN-BCK-014).

    Strict `done`-only per §8.1 ("only when all depends_on are done"): a blocker
    in any other state — including a terminal `cancelled` — leaves the dependent
    blocked. That is deliberate (a dependent must not build on abandoned work); a
    caller that wants to unblock removes the edge or cancels the dependent."""
    result = await conn.execute(
        text(
            "SELECT NOT EXISTS ("
            "  SELECT 1 FROM task_dependencies td JOIN tasks d ON d.id = td.depends_on_id "
            "  WHERE td.task_id = :tid AND d.status <> 'done')"
        ),
        {"tid": task_id},
    )
    return bool(result.scalar_one())


async def backlog_dependents(conn: AsyncConnection, task_id: uuid.UUID) -> Sequence[uuid.UUID]:
    """Ids of still-in-backlog tasks that depend on ``task_id`` — the candidates
    to re-evaluate for readiness when ``task_id`` completes (TITAN-BCK-014)."""
    result = await conn.execute(
        text(
            "SELECT t.id FROM tasks t JOIN task_dependencies td ON td.task_id = t.id "
            "WHERE td.depends_on_id = :tid AND t.status = 'backlog'"
        ),
        {"tid": task_id},
    )
    return [row.id for row in result.all()]


async def ready_pool_rows(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    """The `ready` pool ordered per TITAN-BCK-013: critical first (priority ASC),
    then biggest unblockers (dependent count DESC), then FIFO (created_at ASC)."""
    result = await conn.execute(
        text(
            f"SELECT {_TASK_COLS} FROM tasks t WHERE t.project_id = :pid AND t.status = 'ready' "
            "ORDER BY t.priority ASC, "
            "(SELECT count(*) FROM task_dependencies td WHERE td.depends_on_id = t.id) DESC, "
            "t.created_at ASC"
        ),
        {"pid": project_id},
    )
    return result.all()


# --- read-only project aggregates ------------------------------------------


async def board_rows(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(
            "SELECT id, title, status, priority, assignee_agent_id, sprint_id "
            "FROM tasks WHERE project_id = :pid ORDER BY priority ASC, created_at ASC"
        ),
        {"pid": project_id},
    )
    return result.all()


async def graph_nodes(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text("SELECT id, title, status FROM tasks WHERE project_id = :pid ORDER BY created_at ASC"),
        {"pid": project_id},
    )
    return result.all()


async def graph_edges(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    # Intra-project edges only: both endpoints must live in this project so the
    # per-project graph never leaks task ids from other projects.
    result = await conn.execute(
        text(
            "SELECT td.task_id, td.depends_on_id FROM task_dependencies td "
            "JOIN tasks t ON t.id = td.task_id AND t.project_id = :pid "
            "JOIN tasks d ON d.id = td.depends_on_id AND d.project_id = :pid"
        ),
        {"pid": project_id},
    )
    return result.all()


# --- delivery pipeline state machine (Vol 6 §2.1, TITAN-WFL-001) ------------

_PIPELINE_COLS = "project_id, current_stage, iteration, status, created_at, updated_at, version"


async def ensure_pipeline_state(
    conn: AsyncConnection, project_id: uuid.UUID, *, initial_stage: str
) -> tuple[Row[Any], bool]:
    """Idempotently create the project's pipeline row at ``initial_stage``.

    Returns ``(row, inserted)`` where ``inserted`` is True ONLY for the caller
    that actually created the row. ``INSERT ... ON CONFLICT DO NOTHING RETURNING``
    returns a row iff this statement inserted (a conflict returns nothing), so two
    concurrent first-initializers see exactly one ``inserted=True`` — that caller
    alone logs the opening 'enter' event, so the append-only ledger never carries a
    duplicate opening row (TITAN-WFL-001)."""
    result = await conn.execute(
        text(
            "INSERT INTO pipeline_states (project_id, current_stage) "
            f"VALUES (:pid, :stage) ON CONFLICT (project_id) DO NOTHING RETURNING {_PIPELINE_COLS}"
        ),
        {"pid": project_id, "stage": initial_stage},
    )
    inserted = result.one_or_none()
    if inserted is not None:
        return inserted, True
    existing = await get_pipeline_state(conn, project_id)
    assert existing is not None  # a conflict means the row is committed by a peer
    return existing, False


async def get_pipeline_state(conn: AsyncConnection, project_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_PIPELINE_COLS} FROM pipeline_states WHERE project_id = :pid"),
        {"pid": project_id},
    )
    return result.one_or_none()


async def advance_pipeline_stage(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    from_stage: str,
    to_stage: str,
) -> Row[Any] | None:
    """Guarded stage advance: applies ONLY while the row is still at
    ``from_stage`` (``WHERE current_stage = :from``), resetting iteration to 0 for
    the new stage. Returns None if the pipeline already left ``from_stage`` — which
    makes the advance race-safe and idempotent (a re-fired advance no-ops)."""
    result = await conn.execute(
        text(
            "UPDATE pipeline_states SET current_stage = :to, iteration = 0, "
            "version = version + 1 WHERE project_id = :pid AND current_stage = :from "
            f"RETURNING {_PIPELINE_COLS}"
        ),
        {"pid": project_id, "from": from_stage, "to": to_stage},
    )
    return result.one_or_none()


async def bump_pipeline_iteration(
    conn: AsyncConnection, project_id: uuid.UUID, *, stage: str
) -> Row[Any] | None:
    """Increment the current stage's iteration counter (a retry / re-entry),
    guarded on the stage so a stale caller cannot bump the wrong stage."""
    result = await conn.execute(
        text(
            "UPDATE pipeline_states SET iteration = iteration + 1, version = version + 1 "
            "WHERE project_id = :pid AND current_stage = :stage "
            f"RETURNING {_PIPELINE_COLS}"
        ),
        {"pid": project_id, "stage": stage},
    )
    return result.one_or_none()


async def set_pipeline_status(
    conn: AsyncConnection, project_id: uuid.UUID, *, status: str
) -> Row[Any] | None:
    result = await conn.execute(
        text(
            "UPDATE pipeline_states SET status = :status, version = version + 1 "
            f"WHERE project_id = :pid RETURNING {_PIPELINE_COLS}"
        ),
        {"pid": project_id, "status": status},
    )
    return result.one_or_none()


async def append_stage_event(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    stage: str,
    event: str,
    iteration: int,
    actor: str,
    detail: str,
) -> None:
    """Append one row to the immutable stage ledger. ``detail`` is a JSON string."""
    await conn.execute(
        text(
            "INSERT INTO pipeline_stage_events "
            "(project_id, stage, event, iteration, actor, detail) "
            "VALUES (:pid, :stage, :event, :iteration, :actor, CAST(:detail AS jsonb))"
        ),
        {
            "pid": project_id,
            "stage": stage,
            "event": event,
            "iteration": iteration,
            "actor": actor,
            "detail": detail,
        },
    )


async def stage_events(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    """The project's full stage history in insert order (ledger replay)."""
    result = await conn.execute(
        text(
            "SELECT id, project_id, stage, event, iteration, actor, detail, at "
            "FROM pipeline_stage_events WHERE project_id = :pid ORDER BY id ASC"
        ),
        {"pid": project_id},
    )
    return result.all()


async def active_pipeline_projects(conn: AsyncConnection) -> Sequence[uuid.UUID]:
    """Project ids whose pipeline is still running — the reconciler's work set."""
    result = await conn.execute(
        text("SELECT project_id FROM pipeline_states WHERE status = 'running'")
    )
    return [row.project_id for row in result.all()]


async def projects_without_pipeline(conn: AsyncConnection) -> Sequence[uuid.UUID]:
    """Non-archived projects that have no pipeline row yet (reconciler seeds them)."""
    result = await conn.execute(
        text(
            "SELECT p.id FROM projects p "
            "LEFT JOIN pipeline_states ps ON ps.project_id = p.id "
            "WHERE ps.project_id IS NULL AND p.status <> 'archived'"
        )
    )
    return [row.id for row in result.all()]
