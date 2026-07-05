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
from titan.gateway.rbac import Requires, RequiresAuth, authorize_in_project
from titan.identity.service import Principal
from titan.orchestrator import service
from titan.orchestrator.service import (
    DependencyCreate,
    DependencyOut,
    TaskCreate,
    TaskOut,
    TaskPatch,
    TransitionRequest,
)
from titan.projects import service as projects

router = APIRouter(prefix="/api/v1", tags=["tasks"])

_CreateInProject = Annotated[
    Principal, Depends(Requires("task:create", project_param="project_id"))
]
_ReadInProject = Annotated[Principal, Depends(Requires("task:read", project_param="project_id"))]
_Authenticated = Annotated[Principal, Depends(RequiresAuth())]


async def _authorize_task(request: Request, task: TaskOut, permission: str) -> None:
    """Object-level authorization for an object-addressed task route: authorize
    against the task's own project (404 out-of-scope, 403 underpowered)."""
    await authorize_in_project(request, task.project_id, permission, resource="task")


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


@router.post("/tasks/{task_id}:transition", response_model=TaskOut)
async def transition_task(
    request: Request,
    task_id: uuid.UUID,
    body: TransitionRequest,
    response: Response,
    principal: _Authenticated,
) -> TaskOut:
    """The ONLY way task state moves (TITAN-BCK-012). The transition table maps
    (status, action) to the target state + the permission this route enforces
    object-side; an illegal pair is a 409 invalid-transition."""
    async with request.app.state.engine.connect() as conn:
        task = await service.get_task(conn, task_id)
    transition = service.transitions.resolve(task.status, body.action)
    if transition is None:
        # Authenticated callers who can't even see the task must still 404, not
        # leak the state machine — object-authorize on a read permission first.
        await _authorize_task(request, task, "task:read")
        raise service.invalid_transition(task.status, body.action)
    await _authorize_task(request, task, transition.required_permission)
    actor_user_id = None if body.actor_agent_id is not None else principal.sub
    async with request.app.state.engine.begin() as conn:
        updated = await service.apply_transition(
            conn,
            task,
            transition,
            actor_agent_id=body.actor_agent_id,
            actor_user_id=actor_user_id,
            comment=body.comment,
        )
    set_etag(response, updated.version)
    return updated


def mount(app: FastAPI) -> None:
    app.include_router(router)
