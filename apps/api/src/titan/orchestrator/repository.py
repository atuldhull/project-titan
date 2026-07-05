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
