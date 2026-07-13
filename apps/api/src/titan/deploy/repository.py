"""Private data access for deployments. SQLAlchemy Core over an
``AsyncConnection``; only ``service`` is importable across the module boundary.
The service layer owns every connection/transaction — these functions never open
or commit one."""

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7

_COLS = (
    "id, project_id, task_id, name, compose_file, status, url, port, services, health, detail, "
    "created_at, updated_at, version"
)


async def upsert_pending(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    task_id: uuid.UUID | None,
    *,
    name: str,
    compose_file: str,
) -> Row[Any]:
    """Get-or-create the ``(project_id, name)`` deployment and reset it to
    ``pending`` for a fresh bring-up. Re-deploying an existing app updates the
    same row (idempotency) — clearing the stale URL/port/health so a failed
    re-deploy never leaves the old reachable URL advertised."""
    result = await conn.execute(
        text(
            "INSERT INTO deployments (id, project_id, task_id, name, compose_file) "
            "VALUES (:id, :pid, :tid, :name, :file) "
            "ON CONFLICT (project_id, name) DO UPDATE SET "
            "task_id = EXCLUDED.task_id, compose_file = EXCLUDED.compose_file, "
            "status = 'pending', url = NULL, port = NULL, health = 'unknown', detail = '', "
            "version = deployments.version + 1 "
            f"RETURNING {_COLS}"
        ),
        {"id": uuid7(), "pid": project_id, "tid": task_id, "name": name, "file": compose_file},
    )
    return result.one()


async def mark_result(
    conn: AsyncConnection,
    deployment_id: uuid.UUID,
    *,
    status: str,
    url: str | None,
    port: int | None,
    services: Sequence[str],
    health: str,
    detail: str,
) -> Row[Any]:
    """Record a terminal bring-up outcome (running or failed) with its reachable
    URL, port, services, and health verdict."""
    result = await conn.execute(
        text(
            "UPDATE deployments SET status = :status, url = :url, port = :port, "
            "services = CAST(:services AS jsonb), health = :health, detail = :detail, "
            "version = version + 1 WHERE id = :id "
            f"RETURNING {_COLS}"
        ),
        {
            "id": deployment_id,
            "status": status,
            "url": url,
            "port": port,
            "services": json.dumps(list(services)),
            "health": health,
            "detail": detail,
        },
    )
    return result.one()


async def mark_failed(conn: AsyncConnection, deployment_id: uuid.UUID, *, detail: str) -> Row[Any]:
    """Mark a deployment failed, clearing any advertised URL/port."""
    result = await conn.execute(
        text(
            "UPDATE deployments SET status = 'failed', url = NULL, port = NULL, "
            "health = 'red', detail = :detail, version = version + 1 WHERE id = :id "
            f"RETURNING {_COLS}"
        ),
        {"id": deployment_id, "detail": detail},
    )
    return result.one()


async def mark_stopped(conn: AsyncConnection, deployment_id: uuid.UUID) -> Row[Any]:
    """Mark a deployment stopped after teardown, clearing its reachable URL."""
    result = await conn.execute(
        text(
            "UPDATE deployments SET status = 'stopped', url = NULL, port = NULL, "
            "health = 'unknown', detail = '', version = version + 1 WHERE id = :id "
            f"RETURNING {_COLS}"
        ),
        {"id": deployment_id},
    )
    return result.one()


async def get(conn: AsyncConnection, deployment_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_COLS} FROM deployments WHERE id = :id"), {"id": deployment_id}
    )
    return result.one_or_none()


async def get_by_name(conn: AsyncConnection, project_id: uuid.UUID, name: str) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_COLS} FROM deployments WHERE project_id = :pid AND name = :name"),
        {"pid": project_id, "name": name},
    )
    return result.one_or_none()


async def list_for_project(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(f"SELECT {_COLS} FROM deployments WHERE project_id = :pid ORDER BY created_at DESC"),
        {"pid": project_id},
    )
    return result.all()
