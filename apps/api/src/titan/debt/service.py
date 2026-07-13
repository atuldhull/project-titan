"""Public service interface of the ``debt`` module (Vol 6 §5, TITAN-WFL-021).

``open_ticket`` records a technical-debt ticket for an accepted constitution
deviation (idempotent on its fingerprint); ``ledger`` exposes the open total + the
tickets for the project dashboard. Only this interface crosses the module boundary;
the models, repository, and router stay private. Pydantic v2 DTOs cross it."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.debt import repository as repo
from titan.gateway.rbac import not_found


class DebtTicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    #: code_review | security | constitution
    source: str
    #: low | medium | high
    severity: str
    title: str
    detail: str
    paydown_proposal: str
    paydown_task_id: uuid.UUID | None
    fingerprint: str
    #: open | paid
    status: str
    created_at: datetime
    updated_at: datetime


class DebtLedger(BaseModel):
    """The project's debt ledger — the open total for the dashboard plus the
    tickets (TITAN-WFL-021)."""

    project_id: uuid.UUID
    open_total: int
    tickets: list[DebtTicketOut]


async def open_ticket(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    source: str,
    title: str,
    fingerprint: str,
    severity: str = "medium",
    detail: str = "",
    paydown_proposal: str = "",
    paydown_task_id: uuid.UUID | None = None,
) -> DebtTicketOut:
    """Open (or reuse, by fingerprint) a debt ticket. The caller supplies a stable
    ``fingerprint`` so the same accepted deviation never accretes duplicates."""
    row = await repo.upsert_ticket(
        conn,
        project_id,
        source=source,
        severity=severity,
        title=title,
        detail=detail,
        paydown_proposal=paydown_proposal or f"Pay down: {title}",
        fingerprint=fingerprint,
        paydown_task_id=paydown_task_id,
    )
    return DebtTicketOut.model_validate(row)


async def resolve_ticket(conn: AsyncConnection, ticket_id: uuid.UUID) -> DebtTicketOut:
    row = await repo.resolve(conn, ticket_id)
    if row is None:
        raise not_found("debt ticket")
    return DebtTicketOut.model_validate(row)


async def ledger(conn: AsyncConnection, project_id: uuid.UUID) -> DebtLedger:
    """The project's debt ledger: the open-ticket total + every ticket, newest first."""
    rows = await repo.list_for_project(conn, project_id)
    open_total = await repo.open_count(conn, project_id)
    return DebtLedger(
        project_id=project_id,
        open_total=open_total,
        tickets=[DebtTicketOut.model_validate(row) for row in rows],
    )


__all__ = ["DebtLedger", "DebtTicketOut", "ledger", "open_ticket", "resolve_ticket"]
