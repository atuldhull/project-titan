"""Planning REST endpoints (Vol 6 §4). Private to the module; main.py attaches it
via mount(). Writes require ``pipeline:advance`` (the PM/operator role); reads
require ``pipeline:read``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request

from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.planning import service
from titan.planning.service import (
    BlockerSweepResult,
    CloseSprintIn,
    CloseSprintResult,
    MilestoneSignalsIn,
    MilestoneStateOut,
    MilestoneSyncResult,
    OpenSprintIn,
    PlanRequest,
    SprintCeremonyOut,
    SprintPlanOut,
    TaskBlockOut,
)
from titan.projects import service as projects

router = APIRouter(prefix="/api/v1", tags=["planning"])

_Advance = Annotated[Principal, Depends(Requires("pipeline:advance", project_param="project_id"))]
_Read = Annotated[Principal, Depends(Requires("pipeline:read", project_param="project_id"))]


@router.post("/projects/{project_id}/sprint-plan:preview", response_model=SprintPlanOut)
async def preview_sprint_plan(
    request: Request, project_id: uuid.UUID, body: PlanRequest, principal: _Read
) -> SprintPlanOut:
    """Partition a TaskGraph into a walking-skeleton-first sprint plan (WFL-015).
    Pure — a 422 names the cyclic/incomplete/over-capacity backlog."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)  # 404 outside scope
    return service.build_plan(body)


@router.post("/projects/{project_id}/sprints:open", response_model=SprintCeremonyOut)
async def open_sprint(
    request: Request, project_id: uuid.UUID, body: OpenSprintIn, principal: _Advance
) -> SprintCeremonyOut:
    """Record the sprint-opening planning ceremony (WFL-016)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
    return await service.open_sprint(
        request.app.state.engine, project_id, body, actor=str(principal.sub)
    )


@router.post("/projects/{project_id}/sprints:close", response_model=CloseSprintResult)
async def close_sprint(
    request: Request, project_id: uuid.UUID, body: CloseSprintIn, principal: _Advance
) -> CloseSprintResult:
    """Record the sprint-closing review + retrospective and recalibrate estimates
    (WFL-016/017)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
    return await service.close_sprint(
        request.app.state.engine, project_id, body, actor=str(principal.sub)
    )


@router.get("/projects/{project_id}/ceremonies", response_model=list[SprintCeremonyOut])
async def list_ceremonies(
    request: Request, project_id: uuid.UUID, principal: _Read
) -> list[SprintCeremonyOut]:
    """Every persisted sprint ceremony for the project (WFL-016)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.list_ceremonies(conn, project_id)


@router.post("/projects/{project_id}/milestone:sync", response_model=MilestoneSyncResult)
async def sync_milestone(
    request: Request, project_id: uuid.UUID, body: MilestoneSignalsIn, principal: _Advance
) -> MilestoneSyncResult:
    """Re-evaluate the milestone from the given signals; a regression reopens the
    relevant pipeline stage (WFL-018)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
    return await service.sync_milestone(
        request.app.state.engine, project_id, body, actor=str(principal.sub)
    )


@router.get("/projects/{project_id}/milestone", response_model=MilestoneStateOut)
async def get_milestone(
    request: Request, project_id: uuid.UUID, principal: _Read
) -> MilestoneStateOut:
    """The project's current milestone (WFL-018)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.get_milestone(conn, project_id)


@router.post("/projects/{project_id}/blockers:sweep", response_model=BlockerSweepResult)
async def sweep_blockers(
    request: Request, project_id: uuid.UUID, principal: _Advance
) -> BlockerSweepResult:
    """Run one blocker-detection + recovery-ladder tick for this project (WFL-019)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
    return await service.sweep_blockers(
        request.app.state.engine, project_id=project_id, actor=str(principal.sub)
    )


@router.get("/projects/{project_id}/blockers", response_model=list[TaskBlockOut])
async def list_blockers(
    request: Request, project_id: uuid.UUID, principal: _Read
) -> list[TaskBlockOut]:
    """Every blocker (open, escalated, or recovered) for the project (WFL-019)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.list_blocks(conn, project_id)


def mount(app: FastAPI) -> None:
    app.include_router(router)
