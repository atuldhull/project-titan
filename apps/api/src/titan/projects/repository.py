"""Private data access for the `projects` module (projects, milestones,
sprints). SQLAlchemy Core text() over an AsyncConnection — the router owns the
transaction, the service owns the DTO mapping. Nothing here crosses the module
boundary (TITAN-BCK-004): only ``service.py`` is importable by other modules.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.gateway.pagination import Keyset

_PROJECT_COLS = (
    "id, name, display_name, brief, status, mode, llm_profile, created_by, created_at, version"
)
_MILESTONE_COLS = "id, project_id, title, due_at, status, created_at, version"
_SPRINT_COLS = "id, project_id, number, goal, starts_at, ends_at, status, created_at, version"


# --- projects --------------------------------------------------------------


async def insert_project(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    name: str,
    display_name: str,
    brief: str,
    mode: str,
    llm_profile: str,
    created_by: uuid.UUID,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO projects (id, name, display_name, brief, mode, llm_profile, created_by) "
            f"VALUES (:id, :name, :display_name, :brief, :mode, :llm_profile, :created_by) "
            f"RETURNING {_PROJECT_COLS}"
        ),
        {
            "id": project_id,
            "name": name,
            "display_name": display_name,
            "brief": brief,
            "mode": mode,
            "llm_profile": llm_profile,
            "created_by": created_by,
        },
    )
    return result.one()


async def get_project(conn: AsyncConnection, project_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_PROJECT_COLS} FROM projects WHERE id = :id"),
        {"id": project_id},
    )
    return result.one_or_none()


async def list_projects(
    conn: AsyncConnection, *, keyset: Keyset, status: str | None
) -> Sequence[Row[Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": keyset.fetch}
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status
    seek, seek_params = keyset.where()
    if seek:
        clauses.append(seek)
        params.update(seek_params)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    result = await conn.execute(
        text(
            f"SELECT {_PROJECT_COLS} FROM projects {where} ORDER BY {keyset.order_by} LIMIT :limit"
        ),
        params,
    )
    return result.all()


async def update_project(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    expected_version: int,
    fields: dict[str, Any],
) -> Row[Any] | None:
    """Version-guarded update; returns None if the row vanished or the version
    moved under us (lost optimistic-lock race → 412)."""
    assignments = ", ".join(f"{col} = :{col}" for col in fields)
    prefix = f"{assignments}, " if assignments else ""
    result = await conn.execute(
        text(
            f"UPDATE projects SET {prefix}version = version + 1 "
            f"WHERE id = :id AND version = :expected RETURNING {_PROJECT_COLS}"
        ),
        {**fields, "id": project_id, "expected": expected_version},
    )
    return result.one_or_none()


async def set_project_status(
    conn: AsyncConnection, project_id: uuid.UUID, status: str
) -> Row[Any] | None:
    """Unconditional status write (used by :archive); bumps version."""
    result = await conn.execute(
        text(
            f"UPDATE projects SET status = :status, version = version + 1 "
            f"WHERE id = :id RETURNING {_PROJECT_COLS}"
        ),
        {"id": project_id, "status": status},
    )
    return result.one_or_none()


# --- milestones ------------------------------------------------------------


async def insert_milestone(
    conn: AsyncConnection,
    *,
    milestone_id: uuid.UUID,
    project_id: uuid.UUID,
    title: str,
    due_at: Any,
    status: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO milestones (id, project_id, title, due_at, status) "
            f"VALUES (:id, :project_id, :title, :due_at, :status) RETURNING {_MILESTONE_COLS}"
        ),
        {
            "id": milestone_id,
            "project_id": project_id,
            "title": title,
            "due_at": due_at,
            "status": status,
        },
    )
    return result.one()


async def get_milestone(conn: AsyncConnection, milestone_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_MILESTONE_COLS} FROM milestones WHERE id = :id"),
        {"id": milestone_id},
    )
    return result.one_or_none()


async def list_milestones(
    conn: AsyncConnection, *, project_id: uuid.UUID, keyset: Keyset
) -> Sequence[Row[Any]]:
    seek, seek_params = keyset.where()
    extra = f"AND {seek}" if seek else ""
    result = await conn.execute(
        text(
            f"SELECT {_MILESTONE_COLS} FROM milestones WHERE project_id = :pid {extra} "
            f"ORDER BY {keyset.order_by} LIMIT :limit"
        ),
        {"pid": project_id, "limit": keyset.fetch, **seek_params},
    )
    return result.all()


async def update_milestone(
    conn: AsyncConnection,
    milestone_id: uuid.UUID,
    expected_version: int,
    fields: dict[str, Any],
) -> Row[Any] | None:
    assignments = ", ".join(f"{col} = :{col}" for col in fields)
    prefix = f"{assignments}, " if assignments else ""
    result = await conn.execute(
        text(
            f"UPDATE milestones SET {prefix}version = version + 1 "
            f"WHERE id = :id AND version = :expected RETURNING {_MILESTONE_COLS}"
        ),
        {**fields, "id": milestone_id, "expected": expected_version},
    )
    return result.one_or_none()


# --- sprints ---------------------------------------------------------------


async def insert_sprint(
    conn: AsyncConnection,
    *,
    sprint_id: uuid.UUID,
    project_id: uuid.UUID,
    number: int,
    goal: str | None,
    starts_at: Any,
    ends_at: Any,
    status: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO sprints (id, project_id, number, goal, starts_at, ends_at, status) "
            f"VALUES (:id, :project_id, :number, :goal, :starts_at, :ends_at, :status) "
            f"RETURNING {_SPRINT_COLS}"
        ),
        {
            "id": sprint_id,
            "project_id": project_id,
            "number": number,
            "goal": goal,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "status": status,
        },
    )
    return result.one()


async def get_sprint(conn: AsyncConnection, sprint_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_SPRINT_COLS} FROM sprints WHERE id = :id"),
        {"id": sprint_id},
    )
    return result.one_or_none()


async def list_sprints(
    conn: AsyncConnection, *, project_id: uuid.UUID, keyset: Keyset
) -> Sequence[Row[Any]]:
    seek, seek_params = keyset.where()
    extra = f"AND {seek}" if seek else ""
    result = await conn.execute(
        text(
            f"SELECT {_SPRINT_COLS} FROM sprints WHERE project_id = :pid {extra} "
            f"ORDER BY {keyset.order_by} LIMIT :limit"
        ),
        {"pid": project_id, "limit": keyset.fetch, **seek_params},
    )
    return result.all()


async def update_sprint(
    conn: AsyncConnection,
    sprint_id: uuid.UUID,
    expected_version: int,
    fields: dict[str, Any],
) -> Row[Any] | None:
    assignments = ", ".join(f"{col} = :{col}" for col in fields)
    prefix = f"{assignments}, " if assignments else ""
    result = await conn.execute(
        text(
            f"UPDATE sprints SET {prefix}version = version + 1 "
            f"WHERE id = :id AND version = :expected RETURNING {_SPRINT_COLS}"
        ),
        {**fields, "id": sprint_id, "expected": expected_version},
    )
    return result.one_or_none()
