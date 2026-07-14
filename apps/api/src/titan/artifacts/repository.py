"""Private data access for the derived-artifacts store. SQLAlchemy Core over an
``AsyncConnection``; only ``service`` is importable across the module boundary."""

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7

_COLS = (
    "id, project_id, kind, artifact_version, content_md, content_json, content_hash, "
    "created_at, updated_at, version"
)


async def current(conn: AsyncConnection, project_id: uuid.UUID, kind: str) -> Row[Any] | None:
    """The latest (max artifact_version) artifact of a kind, or None."""
    result = await conn.execute(
        text(
            f"SELECT {_COLS} FROM derived_artifacts WHERE project_id = :pid AND kind = :kind "
            "ORDER BY artifact_version DESC LIMIT 1"
        ),
        {"pid": project_id, "kind": kind},
    )
    return result.one_or_none()


async def list_current(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    """The current version of every artifact kind for the project (one row per kind)."""
    result = await conn.execute(
        text(
            "SELECT DISTINCT ON (kind) "
            f"{_COLS} FROM derived_artifacts WHERE project_id = :pid "
            "ORDER BY kind, artifact_version DESC"
        ),
        {"pid": project_id},
    )
    return result.all()


async def acquire_kind_lock(conn: AsyncConnection, project_id: uuid.UUID, kind: str) -> None:
    """Serialize concurrent regenerations of the same (project, kind) within the
    caller's transaction (a transaction-scoped advisory lock), so the MAX+1
    artifact_version computation in :func:`insert_version` cannot race a second
    regeneration into a duplicate-key violation on uq_derived_artifacts_pkv."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:pid), hashtext(:kind))"),
        {"pid": str(project_id), "kind": kind},
    )


async def insert_version(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    kind: str,
    content_md: str,
    content_json: dict[str, Any],
    content_hash: str,
) -> Row[Any]:
    """Append the next version for (project, kind), artifact_version = max+1. Callers
    MUST hold :func:`acquire_kind_lock` first — the plain INSERT…SELECT MAX aggregate
    takes no row lock, so the advisory lock is what serializes concurrent writers."""
    result = await conn.execute(
        text(
            "INSERT INTO derived_artifacts "
            "(id, project_id, kind, artifact_version, content_md, content_json, content_hash) "
            "SELECT :id, :pid, :kind, "
            "  COALESCE(MAX(artifact_version), 0) + 1, :md, CAST(:cj AS jsonb), :hash "
            "FROM derived_artifacts WHERE project_id = :pid AND kind = :kind "
            f"RETURNING {_COLS}"
        ),
        {
            "id": uuid7(),
            "pid": project_id,
            "kind": kind,
            "md": content_md,
            "cj": json.dumps(content_json),
            "hash": content_hash,
        },
    )
    return result.one()


async def active_project_ids(conn: AsyncConnection) -> Sequence[uuid.UUID]:
    """Projects that have entered their delivery pipeline — the reconciler's scope.
    A read model over pipeline_states (never a write into another domain)."""
    result = await conn.execute(text("SELECT project_id FROM pipeline_states ORDER BY project_id"))
    return [row.project_id for row in result.all()]


async def delete_for_project(conn: AsyncConnection, project_id: uuid.UUID) -> int:
    """Drop every artifact row for a project (the replay-DoD reset). Returns the
    number of rows removed."""
    result = await conn.execute(
        text("DELETE FROM derived_artifacts WHERE project_id = :pid"), {"pid": project_id}
    )
    return int(result.rowcount or 0)
