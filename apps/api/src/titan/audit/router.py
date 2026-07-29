"""Audit read + chain-verify API (Vol 8 §10, SEC-091/094). Mounted by main.py.

``audit`` is a SENSITIVE resource (gateway/rbac.py) — reads do NOT ride ``*:read``;
only an explicit ``audit:read`` grant (Owner/Admin, §4.2) reaches this. The ledger
has no write endpoint: it is written ONLY through the in-process audit client.
"""

from __future__ import annotations

import datetime
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from pydantic import BaseModel

from titan.audit import repository as repo
from titan.audit import service as audit_service
from titan.audit.service import AuditEventOut
from titan.gateway.rbac import Requires
from titan.identity.service import Principal

router = APIRouter(prefix="/api/v1", tags=["audit"])

_AuditReader = Annotated[Principal, Depends(Requires("audit:read"))]


class ChainStatus(BaseModel):
    intact: bool
    broken_seq: int | None


def _to_out(row: Any) -> AuditEventOut:
    detail = row.detail
    if isinstance(detail, str):
        detail = json.loads(detail)
    return AuditEventOut(
        id=row.id,
        seq=int(row.seq),
        ts=row.created_at,
        actor=row.actor,
        org=row.org,
        project_id=row.project_id,
        task_id=row.task_id,
        action=row.action,
        resource=row.resource,
        outcome=row.outcome,
        source_ip=row.source_ip,
        trace_id=row.trace_id,
        detail=detail,
        event_hash=row.event_hash,
    )


@router.get("/audit", response_model=list[AuditEventOut])
async def search_audit(
    request: Request,
    principal: _AuditReader,
    actor: str | None = None,
    action: str | None = None,
    resource: str | None = None,
    since: datetime.datetime | None = None,
    until: datetime.datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[AuditEventOut]:
    """SEC-094 admin search over actor / action / resource / time, newest first."""
    async with request.app.state.engine.connect() as conn:
        rows = await repo.search(
            conn,
            actor=actor,
            action=action,
            resource=resource,
            since=since,
            until=until,
            limit=limit,
        )
    return [_to_out(r) for r in rows]


@router.get("/audit/verify", response_model=ChainStatus)
async def verify_audit_chain(request: Request, principal: _AuditReader) -> ChainStatus:
    """On-demand tamper check (SEC-091): walk the hash chain and report the first
    broken seq, if any. The daily job runs this and alerts on a break."""
    broken = await audit_service.verify_chain(request.app.state.engine)
    return ChainStatus(intact=broken is None, broken_seq=broken)


def mount(app: FastAPI) -> None:
    app.include_router(router)
