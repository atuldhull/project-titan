"""Private data access for the `agents` module. SQLAlchemy Core over an
AsyncConnection; only ``service.py`` crosses the module boundary."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.gateway.pagination import Keyset

_AGENT_COLS = (
    "id, project_id, role, name, status, model_binding, config, "
    "current_task_id, created_at, version"
)


async def insert_agent(
    conn: AsyncConnection,
    *,
    agent_id: uuid.UUID,
    project_id: uuid.UUID | None,
    role: str,
    name: str,
    model_binding: str,
    config: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO agents (id, project_id, role, name, model_binding, config) "
            f"VALUES (:id, :project_id, :role, :name, CAST(:model_binding AS jsonb), "
            f"CAST(:config AS jsonb)) RETURNING {_AGENT_COLS}"
        ),
        {
            "id": agent_id,
            "project_id": project_id,
            "role": role,
            "name": name,
            "model_binding": model_binding,
            "config": config,
        },
    )
    return result.one()


async def get_agent(conn: AsyncConnection, agent_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_AGENT_COLS} FROM agents WHERE id = :id"), {"id": agent_id}
    )
    return result.one_or_none()


async def list_agents(
    conn: AsyncConnection, *, keyset: Keyset, role: str | None, status: str | None
) -> Sequence[Row[Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": keyset.fetch}
    if role is not None:
        clauses.append("role = :role")
        params["role"] = role
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status
    seek, seek_params = keyset.where()
    if seek:
        clauses.append(seek)
        params.update(seek_params)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    result = await conn.execute(
        text(f"SELECT {_AGENT_COLS} FROM agents {where} ORDER BY {keyset.order_by} LIMIT :limit"),
        params,
    )
    return result.all()


async def update_agent(
    conn: AsyncConnection, agent_id: uuid.UUID, expected_version: int, fields: dict[str, Any]
) -> Row[Any] | None:
    assignments = ", ".join(
        f"{col} = CAST(:{col} AS jsonb)"
        if col in ("model_binding", "config")
        else f"{col} = :{col}"
        for col in fields
    )
    prefix = f"{assignments}, " if assignments else ""
    result = await conn.execute(
        text(
            f"UPDATE agents SET {prefix}version = version + 1 "
            f"WHERE id = :id AND version = :expected RETURNING {_AGENT_COLS}"
        ),
        {**fields, "id": agent_id, "expected": expected_version},
    )
    return result.one_or_none()


async def transition_status(
    conn: AsyncConnection, agent_id: uuid.UUID, *, target: str, allowed_from: Sequence[str]
) -> Row[Any] | None:
    """Move an agent to ``target`` only from an ``allowed_from`` status; returns
    None if the row is missing OR the current status is not allowed (the caller
    distinguishes 404 vs 409 by re-reading existence)."""
    result = await conn.execute(
        text(
            f"UPDATE agents SET status = :target, version = version + 1 "
            f"WHERE id = :id AND status = ANY(:allowed) RETURNING {_AGENT_COLS}"
        ),
        {"id": agent_id, "target": target, "allowed": list(allowed_from)},
    )
    return result.one_or_none()
