"""Private data access for the debt ledger. SQLAlchemy Core over an
``AsyncConnection``; only ``service`` is importable across the module boundary."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7

_COLS = (
    "id, project_id, source, severity, title, detail, paydown_proposal, paydown_task_id, "
    "fingerprint, status, created_at, updated_at, version"
)


async def upsert_ticket(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    source: str,
    severity: str,
    title: str,
    detail: str,
    paydown_proposal: str,
    fingerprint: str,
    paydown_task_id: uuid.UUID | None,
) -> Row[Any]:
    """Get-or-create the ``(project_id, fingerprint)`` ticket. Idempotent: a re-run
    gate that accepts the same deviation reuses the existing ticket (its status and
    any linked paydown task are preserved)."""
    result = await conn.execute(
        text(
            "INSERT INTO debt_tickets "
            "(id, project_id, source, severity, title, detail, paydown_proposal, "
            "paydown_task_id, fingerprint) "
            "VALUES (:id, :pid, :source, :sev, :title, :detail, :prop, :ptid, :fp) "
            "ON CONFLICT (project_id, fingerprint) DO UPDATE SET "
            "title = EXCLUDED.title, detail = EXCLUDED.detail, "
            "paydown_proposal = EXCLUDED.paydown_proposal, version = debt_tickets.version + 1 "
            f"RETURNING {_COLS}"
        ),
        {
            "id": uuid7(),
            "pid": project_id,
            "source": source,
            "sev": severity,
            "title": title,
            "detail": detail,
            "prop": paydown_proposal,
            "ptid": paydown_task_id,
            "fp": fingerprint,
        },
    )
    return result.one()


async def resolve(conn: AsyncConnection, ticket_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(
            "UPDATE debt_tickets SET status = 'paid', version = version + 1 "
            f"WHERE id = :id RETURNING {_COLS}"
        ),
        {"id": ticket_id},
    )
    return result.one_or_none()


async def list_for_project(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(f"SELECT {_COLS} FROM debt_tickets WHERE project_id = :pid ORDER BY created_at DESC"),
        {"pid": project_id},
    )
    return result.all()


async def open_count(conn: AsyncConnection, project_id: uuid.UUID) -> int:
    result = await conn.execute(
        text("SELECT count(*) FROM debt_tickets WHERE project_id = :pid AND status = 'open'"),
        {"pid": project_id},
    )
    return int(result.scalar_one())
