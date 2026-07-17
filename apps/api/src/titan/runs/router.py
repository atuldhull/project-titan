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
from titan.gateway.rbac import Requires, RequiresAuth, authorize_in_project
from titan.identity.service import Principal
from titan.orchestrator import service as tasks
from titan.projects import service as projects
from titan.runs import service
from titan.runs.service import ArtifactOut, RunCreate, RunOut

router = APIRouter(prefix="/api/v1", tags=["runs"])

_Authenticated = Annotated[Principal, Depends(RequiresAuth())]
# The project-scoped collection carries the project in its path, so it is guarded
# declaratively like every other /projects/{project_id}/… route rather than
# resolving the project through a task.
_ReadInProject = Annotated[Principal, Depends(Requires("run:read", project_param="project_id"))]


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


@router.get("/projects/{project_id}/runs", response_model=Page[RunOut])
async def list_project_runs(
    request: Request,
    project_id: uuid.UUID,
    pagination: Pagination,
    principal: _ReadInProject,
    agent_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> Page[RunOut]:
    """A project's runs, newest first — the only way to ask what an *agent* is
    doing.

    ``GET /runs`` requires a ``task_id``, which answers "what happened to this
    task"; nothing answered "what is this agent working on". That question needs
    runs, not agents: ``agents.current_task_id`` has no writer anywhere and
    ``agents.status`` only ever holds ``idle`` or ``paused``, whereas a run
    carries the agent's task, the model and provider actually in use, its token
    counts, its start time and its error text.

    Project-scoped on purpose. A run authorizes through its project (TITAN-SEC-032)
    and agents are org-level, so an ``/runs?agent_id=`` filter alone would span
    projects the caller may not read.
    """
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)  # 404 before an empty page
        return await service.list_project_runs(
            conn, project_id=project_id, agent_id=agent_id, status=status, keyset=keyset
        )


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
