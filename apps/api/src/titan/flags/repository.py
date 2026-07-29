"""Private data access for the `flags` module. SQLAlchemy Core over an
AsyncConnection; only ``service.py`` is importable across the module boundary
(TITAN-BCK-001). JSONB ``allowlist`` is bound as a CAST'd JSON string (the same
shape the outbox uses), so no ORM session is needed.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

_FLAG_COLS = (
    "id, key, description, flag_type, flag_class, scope, enabled, percentage, "
    "allowlist, value, owner, expires_at, created_at, updated_at, version"
)

# Columns an update may change — a whitelist so a dynamic SET can never be
# driven by an attacker-supplied column name.
_UPDATABLE = frozenset(
    {
        "description",
        "flag_type",
        "flag_class",
        "scope",
        "enabled",
        "percentage",
        "allowlist",
        "value",
        "owner",
        "expires_at",
    }
)


async def list_flags(conn: AsyncConnection) -> Sequence[Row[Any]]:
    result = await conn.execute(text(f"SELECT {_FLAG_COLS} FROM feature_flags ORDER BY key"))
    return result.all()


async def get_flag(conn: AsyncConnection, key: str) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_FLAG_COLS} FROM feature_flags WHERE key = :key"), {"key": key}
    )
    return result.one_or_none()


async def insert_flag(
    conn: AsyncConnection,
    *,
    flag_id: uuid.UUID,
    key: str,
    description: str,
    flag_type: str,
    flag_class: str,
    scope: str,
    enabled: bool,
    percentage: int | None,
    allowlist: list[str],
    value: str | None,
    owner: str,
    expires_at: datetime.datetime | None,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO feature_flags "
            f"(id, key, description, flag_type, flag_class, scope, enabled, percentage, "
            f"allowlist, value, owner, expires_at) "
            f"VALUES (:id, :key, :description, :flag_type, :flag_class, :scope, :enabled, "
            f":percentage, CAST(:allowlist AS jsonb), :value, :owner, :expires_at) "
            f"RETURNING {_FLAG_COLS}"
        ),
        {
            "id": flag_id,
            "key": key,
            "description": description,
            "flag_type": flag_type,
            "flag_class": flag_class,
            "scope": scope,
            "enabled": enabled,
            "percentage": percentage,
            "allowlist": json.dumps(allowlist),
            "value": value,
            "owner": owner,
            "expires_at": expires_at,
        },
    )
    return result.one()


async def update_flag(conn: AsyncConnection, key: str, changes: dict[str, Any]) -> Row[Any] | None:
    """Apply a partial update. Only whitelisted columns are settable; ``allowlist``
    is re-cast to jsonb. Returns the updated row, or None if the key is unknown."""
    fields = {k: v for k, v in changes.items() if k in _UPDATABLE}
    if not fields:
        return await get_flag(conn, key)
    assignments: list[str] = []
    params: dict[str, Any] = {"key": key}
    for column, val in fields.items():
        if column == "allowlist":
            assignments.append("allowlist = CAST(:allowlist AS jsonb)")
            params["allowlist"] = json.dumps(val)
        else:
            assignments.append(f"{column} = :{column}")
            params[column] = val
    result = await conn.execute(
        text(
            f"UPDATE feature_flags SET {', '.join(assignments)} "
            f"WHERE key = :key RETURNING {_FLAG_COLS}"
        ),
        params,
    )
    return result.one_or_none()


async def delete_flag(conn: AsyncConnection, key: str) -> bool:
    result = await conn.execute(
        text("DELETE FROM feature_flags WHERE key = :key RETURNING 1"), {"key": key}
    )
    return result.one_or_none() is not None
