"""Tool-approval REST endpoints (Vol 7 §4.4, TITAN-INT-022): the human
resolution path for admin-risk tool calls, queryable by the dashboard.

Project-scoped and guarded by the Phase-9 Requires dependency:
``tool_approval:read`` to list pending items, ``tool_approval:write`` to
approve/deny. The audit ledger itself is never exposed here (its read path is
``audit:read``, Owner/Admin only). Private to the module; main.py mounts it.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict

from titan.gateway.pagination import Page, Pagination, keyset_from
from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.projects import service as projects
from titan.tools import service
from titan.tools.approvals import ToolApprovalOut

router = APIRouter(prefix="/api/v1", tags=["tool-approvals"])

_ReadApprovals = Annotated[
    Principal, Depends(Requires("tool_approval:read", project_param="project_id"))
]
_WriteApprovals = Annotated[
    Principal, Depends(Requires("tool_approval:write", project_param="project_id"))
]


class ApprovalResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/projects/{project_id}/tool-approvals", response_model=Page[ToolApprovalOut])
async def list_tool_approvals(
    request: Request,
    project_id: uuid.UUID,
    pagination: Pagination,
    principal: _ReadApprovals,
    status: Annotated[str | None, Query()] = None,
) -> Page[ToolApprovalOut]:
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        rows = await service.list_approvals(conn, project_id, keyset=keyset, status=status)
    page, cursor = keyset.paginate(rows)
    return Page(items=[ToolApprovalOut.model_validate(r) for r in page], next_cursor=cursor)


@router.post(
    "/projects/{project_id}/tool-approvals/{approval_id}:approve",
    response_model=ToolApprovalOut,
)
async def approve_tool_call(
    request: Request,
    project_id: uuid.UUID,
    approval_id: uuid.UUID,
    _body: ApprovalResolution,
    principal: _WriteApprovals,
) -> ToolApprovalOut:
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)
        return await service.resolve_approval(
            conn,
            approval_id=approval_id,
            project_id=project_id,
            action="approved",
            resolved_by=principal.sub,
        )


@router.post(
    "/projects/{project_id}/tool-approvals/{approval_id}:deny",
    response_model=ToolApprovalOut,
)
async def deny_tool_call(
    request: Request,
    project_id: uuid.UUID,
    approval_id: uuid.UUID,
    _body: ApprovalResolution,
    principal: _WriteApprovals,
) -> ToolApprovalOut:
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)
        return await service.resolve_approval(
            conn,
            approval_id=approval_id,
            project_id=project_id,
            action="denied",
            resolved_by=principal.sub,
        )


# --- Admin egress opt-in (Phase 23; SEC-033/043) -----------------------------


class EgressHostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str


class EgressHostOut(BaseModel):
    host: str
    approved_by: uuid.UUID | None


@router.get("/projects/{project_id}/egress-hosts", response_model=list[EgressHostOut])
async def list_egress_hosts(
    request: Request, project_id: uuid.UUID, principal: _ReadApprovals
) -> list[EgressHostOut]:
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        hosts = await service.list_approved_hosts(conn, project_id)
    return [EgressHostOut(host=h.host, approved_by=h.approved_by) for h in hosts]


@router.post("/projects/{project_id}/egress-hosts", response_model=EgressHostOut, status_code=201)
async def add_egress_host(
    request: Request, project_id: uuid.UUID, body: EgressHostIn, principal: _WriteApprovals
) -> EgressHostOut:
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)
        await service.add_approved_host(conn, project_id, body.host, approved_by=principal.sub)
    return EgressHostOut(host=body.host.strip().lower(), approved_by=principal.sub)


@router.delete("/projects/{project_id}/egress-hosts/{host}", status_code=204)
async def remove_egress_host(
    request: Request, project_id: uuid.UUID, host: str, principal: _WriteApprovals
) -> None:
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)
        await service.remove_approved_host(conn, project_id, host)


def mount(app: FastAPI) -> None:
    app.include_router(router)
