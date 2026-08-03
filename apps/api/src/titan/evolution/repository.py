"""Data access for the ``evolution_artifacts`` ledger (TITAN-EVO-002).

Connection-scoped functions (the caller owns the transaction), mirroring the
other repositories. Only immutable references are stored — never prompt text.
"""

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.evolution.models import EvolutionArtifact

_T = cast(Table, EvolutionArtifact.__table__)


async def record_artifact(
    conn: AsyncConnection,
    *,
    kind: str,
    ref: str,
    path: str,
    content_sha256: str,
    commit_sha: str | None,
    semver: str,
    ep: str,
    approved_by: uuid.UUID | None,
    approved_at: datetime | None,
    diff_url: str = "",
) -> None:
    """Insert (or, for the same bytes, refresh the approval of) one artifact row.

    Idempotent on ``(path, content_sha256)``: syncing the manifest then approving
    the same bytes updates the approval signature in place rather than duplicating
    the immutable reference."""
    stmt = insert(_T).values(
        kind=kind,
        ref=ref,
        path=path,
        content_sha256=content_sha256,
        commit_sha=commit_sha,
        semver=semver,
        ep=ep,
        approved_by=approved_by,
        approved_at=approved_at,
        diff_url=diff_url,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_evolution_artifacts_path_sha",
        set_={
            "commit_sha": stmt.excluded.commit_sha,
            "semver": stmt.excluded.semver,
            "ep": stmt.excluded.ep,
            "approved_by": stmt.excluded.approved_by,
            "approved_at": stmt.excluded.approved_at,
            "diff_url": stmt.excluded.diff_url,
        },
    )
    await conn.execute(stmt)


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "ref": row["ref"],
        "path": row["path"],
        "content_sha256": row["content_sha256"],
        "commit_sha": row["commit_sha"],
        "semver": row["semver"],
        "ep": row["ep"],
        "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
        "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
        "diff_url": row["diff_url"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def list_artifacts(
    conn: AsyncConnection,
    *,
    kind: str | None = None,
    ref: str | None = None,
    approved_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    stmt = select(_T)
    if kind is not None:
        stmt = stmt.where(_T.c.kind == kind)
    if ref is not None:
        stmt = stmt.where(_T.c.ref == ref)
    if approved_only:
        stmt = stmt.where(_T.c.approved_by.is_not(None))
    stmt = stmt.order_by(_T.c.created_at.desc()).limit(limit)
    result = await conn.execute(stmt)
    return [_row_dict(r._mapping) for r in result.all()]


async def get_artifact(
    conn: AsyncConnection, *, path: str, content_sha256: str
) -> dict[str, Any] | None:
    stmt = select(_T).where(_T.c.path == path, _T.c.content_sha256 == content_sha256)
    row = (await conn.execute(stmt)).first()
    return _row_dict(row._mapping) if row is not None else None


__all__ = [
    "get_artifact",
    "list_artifacts",
    "record_artifact",
]
