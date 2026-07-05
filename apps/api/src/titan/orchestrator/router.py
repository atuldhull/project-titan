"""Task REST endpoints (§4.2), built on the Phase-7 conventions framework and
guarded by the Phase-9 Requires dependency. Private to the module; main.py
attaches it via mount().

Two shapes of route:
- Project-nested (``/projects/{project_id}/tasks``) carry the project id in the
  path, so ``Requires(project_param=...)`` scopes authorization directly.
- Object-addressed (``/tasks/{task_id}``) do NOT: the project is only known
  after the row is loaded. These authenticate with RequiresAuth (satisfying the
  route-guard lint) and then enforce the real permission object-side against the
  task's own project — which correctly serves org-wide AND project-scoped
  principals and 404s anything outside the caller's scope (TITAN-SEC-032).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response

from titan.gateway.concurrency import require_if_match, set_etag, version_conflict
from titan.gateway.pagination import Page, Pagination, keyset_from
from titan.gateway.rbac import (
    Requires,
    RequiresAuth,
    forbidden,
    get_resolver,
    not_found,
    permission_granted,
)
from titan.identity.service import Principal
from titan.orchestrator import service
from titan.orchestrator.service import (
    DependencyCreate,
    DependencyOut,
    TaskCreate,
    TaskOut,
    TaskPatch,
)
from titan.projects import service as projects

router = APIRouter(prefix="/api/v1", tags=["tasks"])

_CreateInProject = Annotated[
    Principal, Depends(Requires("task:create", project_param="project_id"))
]
_ReadInProject = Annotated[Principal, Depends(Requires("task:read", project_param="project_id"))]
_Authenticated = Annotated[Principal, Depends(RequiresAuth())]


async def _authorize_task(request: Request, task: TaskOut, permission: str) -> None:
    """Object-level authorization for an object-addressed task route.

    Resolves the caller's permissions IN THE TASK'S OWN PROJECT: no visibility
    there is a 404 (existence never leaked), visible-but-underpowered is a 403.
    """
    resolver = get_resolver(request)
    granted = await resolver.project_permissions(task.project_id)
    if not granted:
        raise not_found("task")
    if not permission_granted(permission, granted):
        raise forbidden(permission)


# --- project-nested task routes --------------------------------------------


@router.post("/projects/{project_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(
    request: Request,
    project_id: uuid.UUID,
    body: TaskCreate,
    response: Response,
    principal: _CreateInProject,
) -> TaskOut:
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)  # 404 before the FK insert
        task = await service.create_task(conn, project_id, body, str(principal.sub))
    set_etag(response, task.version)
    return task


@router.get("/projects/{project_id}/tasks", response_model=Page[TaskOut])
async def list_tasks(
    request: Request,
    project_id: uuid.UUID,
    pagination: Pagination,
    principal: _ReadInProject,
    status: Annotated[str | None, Query()] = None,
) -> Page[TaskOut]:
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.list_tasks(conn, project_id=project_id, keyset=keyset, status=status)


# --- object-addressed task routes ------------------------------------------


@router.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(
    request: Request, task_id: uuid.UUID, response: Response, principal: _Authenticated
) -> TaskOut:
    async with request.app.state.engine.connect() as conn:
        task = await service.get_task(conn, task_id)
        await _authorize_task(request, task, "task:read")
    set_etag(response, task.version)
    return task


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    request: Request,
    task_id: uuid.UUID,
    body: TaskPatch,
    response: Response,
    principal: _Authenticated,
) -> TaskOut:
    async with request.app.state.engine.begin() as conn:
        current = await service.get_task(conn, task_id)
        await _authorize_task(request, current, "task:update")
        require_if_match(request, current.version)
        updated = await service.update_task(
            conn, task_id, body, current.version, str(principal.sub)
        )
    if updated is None:
        raise version_conflict()
    set_etag(response, updated.version)
    return updated


@router.post("/tasks/{task_id}/dependencies", response_model=DependencyOut, status_code=201)
async def add_dependency(
    request: Request, task_id: uuid.UUID, body: DependencyCreate, principal: _Authenticated
) -> DependencyOut:
    async with request.app.state.engine.begin() as conn:
        task = await service.get_task(conn, task_id)
        await _authorize_task(request, task, "task:update")
        # To depend on another task you must be able to SEE it; an invisible
        # target 404s exactly like a missing one (no cross-project probing).
        target = await service.get_task(conn, body.depends_on_id)
        await _authorize_task(request, target, "task:read")
        return await service.add_dependency(
            conn, task_id=task_id, depends_on_id=body.depends_on_id, actor=str(principal.sub)
        )


def mount(app: FastAPI) -> None:
    app.include_router(router)
