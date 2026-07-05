"""Project/roadmap REST endpoints (§4.2), built on the Phase-7 conventions
framework and guarded by the Phase-9 Requires dependency. Private to the
module; main.py attaches it via mount().

The /board and /graph aggregates read task data, which the projects module does
not own — they delegate to ``orchestrator.service`` (the one allowed
cross-module channel, TITAN-BCK-004), so this module never touches the tasks
tables directly.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response

from titan.gateway.concurrency import require_if_match, set_etag, version_conflict
from titan.gateway.pagination import Page, Pagination, keyset_from
from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.orchestrator import service as tasks
from titan.projects import service
from titan.projects.service import (
    MilestoneCreate,
    MilestoneOut,
    MilestonePatch,
    ProjectCreate,
    ProjectOut,
    ProjectPatch,
    SprintCreate,
    SprintOut,
    SprintPatch,
)

router = APIRouter(prefix="/api/v1", tags=["projects"])

# Guards. Creation and listing are org-scoped; per-project routes carry the
# project id in the path so project-level role grants are consulted.
_WriteOrg = Annotated[Principal, Depends(Requires("project:write"))]
_ReadOrg = Annotated[Principal, Depends(Requires("project:read"))]
_WriteProject = Annotated[Principal, Depends(Requires("project:write", project_param="project_id"))]
_ReadProject = Annotated[Principal, Depends(Requires("project:read", project_param="project_id"))]
# Board/graph expose task data, so they require task:read (via *:read).
_ReadTasks = Annotated[Principal, Depends(Requires("task:read", project_param="project_id"))]


# --- projects --------------------------------------------------------------


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(
    request: Request, body: ProjectCreate, response: Response, principal: _WriteOrg
) -> ProjectOut:
    async with request.app.state.engine.begin() as conn:
        project = await service.create_project(conn, body, principal.sub)
    set_etag(response, project.version)
    return project


@router.get("/projects", response_model=Page[ProjectOut])
async def list_projects(
    request: Request,
    pagination: Pagination,
    principal: _ReadOrg,
    status: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query(pattern=r"^-?created_at$")] = "-created_at",
) -> Page[ProjectOut]:
    keyset = keyset_from(pagination, descending=sort.startswith("-"))
    async with request.app.state.engine.connect() as conn:
        return await service.list_projects(conn, keyset=keyset, status=status)


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(
    request: Request, project_id: uuid.UUID, response: Response, principal: _ReadProject
) -> ProjectOut:
    async with request.app.state.engine.connect() as conn:
        project = await service.get_project(conn, project_id)
    set_etag(response, project.version)
    return project


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def update_project(
    request: Request,
    project_id: uuid.UUID,
    body: ProjectPatch,
    response: Response,
    principal: _WriteProject,
) -> ProjectOut:
    async with request.app.state.engine.begin() as conn:
        current = await service.get_project(conn, project_id)
        require_if_match(request, current.version)
        updated = await service.update_project(conn, project_id, body, current.version)
    if updated is None:
        raise version_conflict()
    set_etag(response, updated.version)
    return updated


@router.post("/projects/{project_id}:archive", response_model=ProjectOut)
async def archive_project(
    request: Request, project_id: uuid.UUID, response: Response, principal: _WriteProject
) -> ProjectOut:
    async with request.app.state.engine.begin() as conn:
        project = await service.archive_project(conn, project_id)
    set_etag(response, project.version)
    return project


@router.get("/projects/{project_id}/board", response_model=tasks.Board)
async def project_board(
    request: Request, project_id: uuid.UUID, principal: _ReadTasks
) -> tasks.Board:
    async with request.app.state.engine.connect() as conn:
        await service.get_project(conn, project_id)  # 404 if the project is gone
        return await tasks.board_for_project(conn, project_id)


@router.get("/projects/{project_id}/graph", response_model=tasks.Graph)
async def project_graph(
    request: Request, project_id: uuid.UUID, principal: _ReadTasks
) -> tasks.Graph:
    async with request.app.state.engine.connect() as conn:
        await service.get_project(conn, project_id)  # 404 if the project is gone
        return await tasks.graph_for_project(conn, project_id)


# --- milestones ------------------------------------------------------------


@router.post("/projects/{project_id}/milestones", response_model=MilestoneOut, status_code=201)
async def create_milestone(
    request: Request,
    project_id: uuid.UUID,
    body: MilestoneCreate,
    response: Response,
    principal: _WriteProject,
) -> MilestoneOut:
    async with request.app.state.engine.begin() as conn:
        await service.get_project(conn, project_id)  # 404 before FK insert
        milestone = await service.create_milestone(conn, project_id, body)
    set_etag(response, milestone.version)
    return milestone


@router.get("/projects/{project_id}/milestones", response_model=Page[MilestoneOut])
async def list_milestones(
    request: Request, project_id: uuid.UUID, pagination: Pagination, principal: _ReadProject
) -> Page[MilestoneOut]:
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        await service.get_project(conn, project_id)
        return await service.list_milestones(conn, project_id=project_id, keyset=keyset)


@router.get("/projects/{project_id}/milestones/{milestone_id}", response_model=MilestoneOut)
async def get_milestone(
    request: Request,
    project_id: uuid.UUID,
    milestone_id: uuid.UUID,
    response: Response,
    principal: _ReadProject,
) -> MilestoneOut:
    async with request.app.state.engine.connect() as conn:
        milestone = await service.get_milestone_in_project(conn, project_id, milestone_id)
    set_etag(response, milestone.version)
    return milestone


@router.patch("/projects/{project_id}/milestones/{milestone_id}", response_model=MilestoneOut)
async def update_milestone(
    request: Request,
    project_id: uuid.UUID,
    milestone_id: uuid.UUID,
    body: MilestonePatch,
    response: Response,
    principal: _WriteProject,
) -> MilestoneOut:
    async with request.app.state.engine.begin() as conn:
        current = await service.get_milestone_in_project(conn, project_id, milestone_id)
        require_if_match(request, current.version)
        updated = await service.update_milestone(conn, milestone_id, body, current.version)
    if updated is None:
        raise version_conflict()
    set_etag(response, updated.version)
    return updated


# --- sprints ---------------------------------------------------------------


@router.post("/projects/{project_id}/sprints", response_model=SprintOut, status_code=201)
async def create_sprint(
    request: Request,
    project_id: uuid.UUID,
    body: SprintCreate,
    response: Response,
    principal: _WriteProject,
) -> SprintOut:
    async with request.app.state.engine.begin() as conn:
        await service.get_project(conn, project_id)  # 404 before FK insert
        sprint = await service.create_sprint(conn, project_id, body, str(principal.sub))
    set_etag(response, sprint.version)
    return sprint


@router.get("/projects/{project_id}/sprints", response_model=Page[SprintOut])
async def list_sprints(
    request: Request, project_id: uuid.UUID, pagination: Pagination, principal: _ReadProject
) -> Page[SprintOut]:
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        await service.get_project(conn, project_id)
        return await service.list_sprints(conn, project_id=project_id, keyset=keyset)


@router.get("/projects/{project_id}/sprints/{sprint_id}", response_model=SprintOut)
async def get_sprint(
    request: Request,
    project_id: uuid.UUID,
    sprint_id: uuid.UUID,
    response: Response,
    principal: _ReadProject,
) -> SprintOut:
    async with request.app.state.engine.connect() as conn:
        sprint = await service.get_sprint_in_project(conn, project_id, sprint_id)
    set_etag(response, sprint.version)
    return sprint


@router.patch("/projects/{project_id}/sprints/{sprint_id}", response_model=SprintOut)
async def update_sprint(
    request: Request,
    project_id: uuid.UUID,
    sprint_id: uuid.UUID,
    body: SprintPatch,
    response: Response,
    principal: _WriteProject,
) -> SprintOut:
    async with request.app.state.engine.begin() as conn:
        current = await service.get_sprint_in_project(conn, project_id, sprint_id)
        require_if_match(request, current.version)
        updated = await service.update_sprint(
            conn, sprint_id, body, current.version, str(principal.sub)
        )
    if updated is None:
        raise version_conflict()
    set_etag(response, updated.version)
    return updated


def mount(app: FastAPI) -> None:
    app.include_router(router)
