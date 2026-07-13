"""Handoff REST endpoints (Vol 6 §2.2–2.3). Private to the module; main.py
attaches it via mount().

Submitting a stage's artifact runs the executor: validate → store → gate →
advance/retry/escalate. The outcome (including a field-level error report on a
schema rejection) is returned in the body; a processed handoff is always 200 —
4xx is reserved for un-processable requests (unknown/out-of-order stage, a
non-running or budget-held pipeline).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Request

from titan.gateway.rbac import Requires
from titan.handoffs import service
from titan.handoffs.service import GateLedger, HandoffArtifactOut, HandoffOutcome, Traceability
from titan.identity.service import Principal
from titan.projects import service as projects

router = APIRouter(prefix="/api/v1", tags=["handoffs"])

_AdvancePipeline = Annotated[
    Principal, Depends(Requires("pipeline:advance", project_param="project_id"))
]
_ReadPipeline = Annotated[Principal, Depends(Requires("pipeline:read", project_param="project_id"))]


@router.post("/projects/{project_id}/handoffs/{stage}", response_model=HandoffOutcome)
async def submit_handoff(
    request: Request,
    project_id: uuid.UUID,
    stage: str,
    principal: _AdvancePipeline,
    body: Annotated[dict[str, Any], Body()],
) -> HandoffOutcome:
    """Submit the producing stage's §2.3 artifact and run its handoff. On a schema
    validation failure the outcome is ``schema_rejected`` with a field-level error
    report; on a gate failure it retries within budget, then escalates."""
    engine = request.app.state.engine
    async with engine.connect() as conn:
        await projects.get_project(conn, project_id)  # 404 before the FK insert
    return await service.submit_artifact(
        engine, project_id, stage, body, submitted_by=str(principal.sub)
    )


@router.get("/projects/{project_id}/handoffs/gate-ledger", response_model=GateLedger)
async def export_gate_ledger(
    request: Request, project_id: uuid.UUID, principal: _ReadPipeline
) -> GateLedger:
    """The complete pass/fail gate ledger for the project (TITAN-WFL-004)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.gate_ledger(conn, project_id)


@router.get("/projects/{project_id}/handoffs/{stage}/artifact", response_model=HandoffArtifactOut)
async def get_latest_artifact(
    request: Request, project_id: uuid.UUID, stage: str, principal: _ReadPipeline
) -> HandoffArtifactOut:
    """The most recent artifact submitted for a stage (404 if none)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.latest_artifact(conn, project_id, stage)


@router.get("/projects/{project_id}/traceability", response_model=Traceability)
async def get_traceability(
    request: Request, project_id: uuid.UUID, principal: _ReadPipeline
) -> Traceability:
    """The auto-generated TITAN-WFL-006 traceability table (brief feature → stories →
    tasks → PRs → tests → docs) with gap detection over the MUST-features."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.traceability(conn, project_id)


def mount(app: FastAPI) -> None:
    app.include_router(router)
