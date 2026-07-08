"""Private data access for the `runtime` module (the watchdog's live config).
SQLAlchemy Core over an AsyncConnection; only ``service.py`` crosses the module
boundary (TITAN-BCK-004)."""

import uuid
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

_CONFIG_COLS = (
    "id, project_id, stalled_after_ms, sweep_interval_ms, reclaim_min_idle_ms, "
    "ack_timeout_overrides, created_at, version"
)


async def get_config_for_project(conn: AsyncConnection, project_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_CONFIG_COLS} FROM watchdog_config WHERE project_id = :pid"),
        {"pid": project_id},
    )
    return result.one_or_none()


async def get_global_config(conn: AsyncConnection) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_CONFIG_COLS} FROM watchdog_config WHERE project_id IS NULL")
    )
    return result.one_or_none()


async def upsert_config(
    conn: AsyncConnection,
    *,
    config_id: uuid.UUID,
    project_id: uuid.UUID | None,
    stalled_after_ms: int,
    sweep_interval_ms: int,
    reclaim_min_idle_ms: int,
    ack_timeout_overrides: str,
) -> Row[Any]:
    """Insert or update the (project or global) watchdog-config row. The COALESCE
    unique index makes the NULL-project default a single upsertable row too."""
    result = await conn.execute(
        text(
            f"INSERT INTO watchdog_config (id, project_id, stalled_after_ms, "
            f"sweep_interval_ms, reclaim_min_idle_ms, ack_timeout_overrides) "
            f"VALUES (:id, :pid, :stalled, :sweep, :reclaim, CAST(:acks AS jsonb)) "
            f"ON CONFLICT (COALESCE(project_id, '00000000-0000-0000-0000-000000000000'::uuid)) "
            f"DO UPDATE SET stalled_after_ms = EXCLUDED.stalled_after_ms, "
            f"sweep_interval_ms = EXCLUDED.sweep_interval_ms, "
            f"reclaim_min_idle_ms = EXCLUDED.reclaim_min_idle_ms, "
            f"ack_timeout_overrides = EXCLUDED.ack_timeout_overrides, "
            f"version = watchdog_config.version + 1 "
            f"RETURNING {_CONFIG_COLS}"
        ),
        {
            "id": config_id,
            "pid": project_id,
            "stalled": stalled_after_ms,
            "sweep": sweep_interval_ms,
            "reclaim": reclaim_min_idle_ms,
            "acks": ack_timeout_overrides,
        },
    )
    return result.one()
