"""Admin-approved per-project egress hosts (Phase 23; TITAN-SEC-033/043).

The forward proxy allows the registry mirror + the task git remote out of the
box; an Admin opts a project into ADDITIONAL hosts here. The proxy consults this
table only on a would-be denial (the rare, off-baseline path), so the hot path
stays offline. The RBAC-gated endpoints are the SEC-033 opt-in surface."""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7


@dataclass(frozen=True)
class EgressHost:
    host: str
    approved_by: uuid.UUID | None
    created_at: str


def _normalize(host: str) -> str:
    return host.strip().rstrip(".").lower()


async def is_host_approved(conn: AsyncConnection, project_id: uuid.UUID, host: str) -> bool:
    """True if ``host`` is an Admin-approved egress host for the project."""
    result = await conn.execute(
        text("SELECT 1 FROM project_egress_hosts WHERE project_id = :p AND host = :h LIMIT 1"),
        {"p": project_id, "h": _normalize(host)},
    )
    return result.scalar_one_or_none() is not None


async def list_approved_hosts(conn: AsyncConnection, project_id: uuid.UUID) -> list[EgressHost]:
    result = await conn.execute(
        text(
            "SELECT host, approved_by, created_at FROM project_egress_hosts "
            "WHERE project_id = :p ORDER BY host"
        ),
        {"p": project_id},
    )
    return [
        EgressHost(host=row.host, approved_by=row.approved_by, created_at=str(row.created_at))
        for row in result
    ]


async def add_approved_host(
    conn: AsyncConnection, project_id: uuid.UUID, host: str, *, approved_by: uuid.UUID | None
) -> None:
    """Idempotently opt the project into ``host`` (SEC-033 admin action)."""
    await conn.execute(
        text(
            "INSERT INTO project_egress_hosts (id, project_id, host, approved_by) "
            "VALUES (:id, :p, :h, :by) ON CONFLICT (project_id, host) DO NOTHING"
        ),
        {"id": uuid7(), "p": project_id, "h": _normalize(host), "by": approved_by},
    )


async def remove_approved_host(conn: AsyncConnection, project_id: uuid.UUID, host: str) -> bool:
    result = await conn.execute(
        text("DELETE FROM project_egress_hosts WHERE project_id = :p AND host = :h"),
        {"p": project_id, "h": _normalize(host)},
    )
    return result.rowcount > 0


__all__ = [
    "EgressHost",
    "add_approved_host",
    "is_host_approved",
    "list_approved_hosts",
    "remove_approved_host",
]
