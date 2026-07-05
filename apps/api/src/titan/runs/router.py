"""Run + artifact REST endpoints (§4.2, §8.2). On the Phase-7 framework, guarded
by Phase-9 Requires. Private to the module; main.py attaches it via mount().

Runs are object-addressed (the path carries a task or run id, not a project), so
authorization resolves the owning project THROUGH the task and enforces it
object-side: out-of-scope is 404, underpowered is 403 (TITAN-SEC-032).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.gateway.concurrency import set_etag
from titan.gateway.pagination import Page, Pagination, keyset_from
from titan.gateway.rbac import RequiresAuth, authorize_in_project
from titan.identity.service import Principal
from titan.orchestrator import service as tasks
from titan.runs import service
from titan.runs.service import ArtifactOut, RunCreate, RunOut

router = APIRouter(prefix="/api/v1", tags=["runs"])

_Authenticated = Annotated[Principal, Depends(RequiresAuth())]


async def _authorize_run(
    request: Request, conn: AsyncConnection, run_id: uuid.UUID, permission: str
) -> RunOut:
    """Load a run and authorize the caller against the run's project (via its
    task). 404 if the run is missing or the project is out of scope."""
    run = await service.get_run(conn, run_id)
    task = await tasks.get_task(conn, run.task_id)
    await authorize_in_project(request, task.project_id, permission, resource="run")
    return run


@router.post("/tasks/{task_id}/runs", response_model=RunOut, status_code=201)
async def create_run(
    request: Request,
    task_id: uuid.UUID,
    body: RunCreate,
    response: Response,
    principal: _Authenticated,
) -> RunOut:
    async with request.app.state.engine.begin() as conn:
        task = await tasks.get_task(conn, task_id)  # 404 if the task is gone
        await authorize_in_project(request, task.project_id, "run:create", resource="task")
        run = await service.create_run(conn, task_id, body)
    set_etag(response, run.version)
    return run


@router.get("/runs", response_model=Page[RunOut])
async def list_runs(
    request: Request,
    pagination: Pagination,
    principal: _Authenticated,
    task_id: Annotated[uuid.UUID, Query()],
) -> Page[RunOut]:
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        task = await tasks.get_task(conn, task_id)
        await authorize_in_project(request, task.project_id, "run:read", resource="task")
        return await service.list_runs(conn, task_id=task_id, keyset=keyset)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    request: Request, run_id: uuid.UUID, response: Response, principal: _Authenticated
) -> RunOut:
    async with request.app.state.engine.connect() as conn:
        run = await _authorize_run(request, conn, run_id, "run:read")
    set_etag(response, run.version)
    return run


@router.post("/runs/{run_id}:cancel", response_model=RunOut)
async def cancel_run(
    request: Request, run_id: uuid.UUID, response: Response, principal: _Authenticated
) -> RunOut:
    async with request.app.state.engine.begin() as conn:
        await _authorize_run(request, conn, run_id, "run:cancel")
        run = await service.cancel_run(conn, run_id)
    set_etag(response, run.version)
    return run


@router.get("/runs/{run_id}/artifacts", response_model=Page[ArtifactOut])
async def list_artifacts(
    request: Request, run_id: uuid.UUID, pagination: Pagination, principal: _Authenticated
) -> Page[ArtifactOut]:
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        await _authorize_run(request, conn, run_id, "run:read")
        return await service.list_artifacts(conn, run_id=run_id, keyset=keyset)


def mount(app: FastAPI) -> None:
    app.include_router(router)
