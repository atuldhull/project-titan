"""Raw-SQL persistence for secret metadata + the audit ledger (Phase 68).

Metadata only — never a value (SEC-050/SEC-051). Same idiom as the rest of the
codebase: module-level ``async def`` over a Core :class:`AsyncConnection` with
``text()`` SQL.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7

_METADATA_COLUMNS = (
    "name, scope, scope_ref, owner_id, rotation_days, rotated_at, created_at, updated_at"
)


async def insert_metadata(
    conn: AsyncConnection,
    *,
    name: str,
    scope: str,
    scope_ref: str | None,
    owner_id: uuid.UUID | None,
    rotation_days: int,
    rotated_at: dt.datetime,
) -> Row[Any]:
    row = (
        await conn.execute(
            text(
                "INSERT INTO secret_metadata "
                "(id, name, scope, scope_ref, owner_id, rotation_days, rotated_at) "
                "VALUES (:id, :name, :scope, :scope_ref, :owner_id, :rotation_days, :rotated_at) "
                f"RETURNING {_METADATA_COLUMNS}"
            ),
            {
                "id": uuid7(),
                "name": name,
                "scope": scope,
                "scope_ref": scope_ref,
                "owner_id": owner_id,
                "rotation_days": rotation_days,
                "rotated_at": rotated_at,
            },
        )
    ).one()
    return row


async def touch_rotated(
    conn: AsyncConnection, name: str, rotated_at: dt.datetime
) -> Row[Any] | None:
    return (
        await conn.execute(
            text(
                "UPDATE secret_metadata SET rotated_at = :rotated_at "
                "WHERE name = :name "
                f"RETURNING {_METADATA_COLUMNS}"
            ),
            {"name": name, "rotated_at": rotated_at},
        )
    ).one_or_none()


async def delete_metadata(conn: AsyncConnection, name: str) -> bool:
    result = await conn.execute(
        text("DELETE FROM secret_metadata WHERE name = :name"), {"name": name}
    )
    return result.rowcount == 1


async def get_metadata(conn: AsyncConnection, name: str) -> Row[Any] | None:
    return (
        await conn.execute(
            text(f"SELECT {_METADATA_COLUMNS} FROM secret_metadata WHERE name = :name"),
            {"name": name},
        )
    ).one_or_none()


async def list_metadata(conn: AsyncConnection) -> Sequence[Row[Any]]:
    return (
        await conn.execute(text(f"SELECT {_METADATA_COLUMNS} FROM secret_metadata ORDER BY name"))
    ).all()


async def insert_audit(
    conn: AsyncConnection,
    *,
    event_type: str,
    actor: str,
    secret_name: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO secret_audit (id, event_type, actor, secret_name, detail) "
            "VALUES (:id, :event_type, :actor, :secret_name, CAST(:detail AS jsonb))"
        ),
        {
            "id": uuid7(),
            "event_type": event_type,
            "actor": actor,
            "secret_name": secret_name,
            "detail": json.dumps(detail or {}),
        },
    )


__all__ = [
    "delete_metadata",
    "get_metadata",
    "insert_audit",
    "insert_metadata",
    "list_metadata",
    "touch_rotated",
]
