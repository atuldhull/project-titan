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

from titan.flags import service as flags_service
from titan.gateway import cache
from titan.gateway.concurrency import require_if_match, set_etag, version_conflict
from titan.gateway.pagination import Page, Pagination, keyset_from
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import Requires, RequiresAuth, authorize_in_project
from titan.identity.service import Principal
from titan.orchestrator import service
from titan.orchestrator.service import (
    DependencyCreate,
    DependencyOut,
    PipelineAdvanceRequest,
    PipelineNotApplicableRequest,
    PipelineOut,
    PipelineRetryRequest,
    PipelineView,
    TaskCreate,
    TaskOut,
    TaskPatch,
    TransitionRequest,
    WorkflowDefinition,
)
from titan.projects import service as projects

router = APIRouter(prefix="/api/v1", tags=["tasks"])

_CreateInProject = Annotated[
    Principal, Depends(Requires("task:create", project_param="project_id"))
]
_ReadInProject = Annotated[Principal, Depends(Requires("task:read", project_param="project_id"))]
_Authenticated = Annotated[Principal, Depends(RequiresAuth())]
# Pipeline guards (Vol 6 §2.1). Reading rides on *:read; advancing needs the
# explicit pipeline:advance; the NOT_APPLICABLE skip and budget-raise need
# pipeline:override — the human/CEO authority (agent-role granularity deferred,
# as with KB approval in Phase 39).
_ReadPipeline = Annotated[Principal, Depends(Requires("pipeline:read", project_param="project_id"))]
_AdvancePipeline = Annotated[
    Principal, Depends(Requires("pipeline:advance", project_param="project_id"))
]
_OverridePipeline = Annotated[
    Principal, Depends(Requires("pipeline:override", project_param="project_id"))
]


async def _authorize_task(request: Request, task: TaskOut, permission: str) -> None:
    """Object-level authorization for an object-addressed task route: authorize
    against the task's own project (404 out-of-scope, 403 underpowered)."""
    await authorize_in_project(request, task.project_id, permission, resource="task")


# --- the workflow definition (Vol 4 §6.3) ----------------------------------


@router.get("/workflow", response_model=WorkflowDefinition)
async def get_workflow(principal: _Authenticated) -> WorkflowDefinition:
    """The task state machine: the board's columns and its legal moves.

    Static, project-independent metadata compiled into the app — no DB read and
    no project in the path to scope against, so this authenticates only. It
    describes the MACHINE, never a task: it discloses nothing about any project's
    data, and every role that can reach the API already holds `*:read`.
    """
    return service.workflow_definition()


# --- project-nested task routes --------------------------------------------


async def _invalidate_board(request: Request, project_id: uuid.UUID) -> None:
    """Bump the project cache version so the read-through board (and project DTO)
    reflect this task write within one poll cycle (TITAN-BCK-023). Fail-open."""
    await cache.bump_version(request.app.state.redis, cache.project_version_key(project_id))


@router.post("/projects/{project_id}/tasks", response_model=TaskOut, status_code=201)
async def create_task(
    request: Request,
    project_id: uuid.UUID,
    body: TaskCreate,
    response: Response,
    principal: _CreateInProject,
) -> TaskOut:
    # TITAN-OPS-020 admission control. Two independent backpressure controls,
    # both BEFORE the ledger write so refused work is never half-committed:
    #  (1) intake.read_only kill switch — the whole platform pauses new intake;
    #  (2) per-project concurrency quota — one project cannot monopolise workers.
    # Both answer 4xx/5xx-that-a-client-retries (not a silent drop); accepted work
    # is always ledgered and simply waits in a queued state (never lost).
    settings = request.app.state.settings
    flags = request.app.state.flags
    # Best-effort: a flag-store blip must not turn intake into a 500 (OPS-020
    # "zero 5xx on intake") — degrade to accepting + ledgering the work.
    if await flags.is_enabled_or(flags_service.INTAKE_READ_ONLY, False):
        raise ProblemException(
            status=503,
            title="Intake is read-only",
            detail="The platform is in read-only mode; new work is paused. Retry later.",
            problem_type=problem_type("intake-read-only"),
            headers={"Retry-After": str(max(1, settings.admission_retry_after_seconds))},
        )
    quota = settings.per_project_concurrent_task_quota
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)  # 404 before the FK insert
        if quota > 0:
            active = await service.count_active_tasks(conn, project_id)
            if active >= quota:
                raise ProblemException(
                    status=429,
                    title="Project concurrency quota exceeded",
                    detail=(
                        f"This project has {active} tasks in flight (limit {quota}); "
                        "retry shortly — accepted work is queued, never dropped."
                    ),
                    problem_type=problem_type("project-quota-exceeded"),
                    headers={"Retry-After": str(max(1, settings.admission_retry_after_seconds))},
                )
        task = await service.create_task(conn, project_id, body, str(principal.sub))
    await _invalidate_board(request, project_id)
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
    await _invalidate_board(request, updated.project_id)
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
            # The TITAN-BRN-002 gate on `approve` reads wm:{task_id}:events.
            redis=request.app.state.redis,
        )
    await _invalidate_board(request, updated.project_id)
    set_etag(response, updated.version)
    return updated


# --- delivery pipeline routes (Vol 6 §2.1, TITAN-WFL-001/002) ---------------


@router.get("/projects/{project_id}/pipeline", response_model=PipelineView)
async def get_pipeline(
    request: Request, project_id: uuid.UUID, principal: _ReadPipeline
) -> PipelineView:
    """The project's pipeline cursor (current stage + iteration) and the full
    append-only stage history. First read lazily initializes the pipeline."""
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)  # 404 before touching pipeline
        return await service.get_pipeline(conn, project_id)


@router.post("/projects/{project_id}/pipeline:advance", response_model=PipelineOut)
async def advance_pipeline(
    request: Request,
    project_id: uuid.UUID,
    body: PipelineAdvanceRequest,
    response: Response,
    principal: _AdvancePipeline,
) -> PipelineOut:
    """Complete the current stage and enter its immediate successor. An
    out-of-order ``target_stage`` is a 409 with a logged rejected-transition event
    and no state change (TITAN-WFL-002)."""
    engine = request.app.state.engine
    async with engine.connect() as conn:
        await projects.get_project(conn, project_id)  # 404 before mutating
    updated = await service.advance_pipeline(engine, project_id, body, actor=str(principal.sub))
    set_etag(response, updated.version)
    return updated


@router.post("/projects/{project_id}/pipeline:not-applicable", response_model=PipelineOut)
async def pipeline_not_applicable(
    request: Request,
    project_id: uuid.UUID,
    body: PipelineNotApplicableRequest,
    response: Response,
    principal: _OverridePipeline,
) -> PipelineOut:
    """Mark the current stage NOT_APPLICABLE with a logged justification — the
    single sanctioned skip (TITAN-WFL-002), gated on pipeline:override."""
    engine = request.app.state.engine
    async with engine.connect() as conn:
        await projects.get_project(conn, project_id)  # 404 before mutating
    updated = await service.mark_stage_not_applicable(
        engine, project_id, body, actor=str(principal.sub)
    )
    set_etag(response, updated.version)
    return updated


@router.post("/projects/{project_id}/pipeline:retry", response_model=PipelineOut)
async def retry_pipeline(
    request: Request,
    project_id: uuid.UUID,
    body: PipelineRetryRequest,
    response: Response,
    principal: _AdvancePipeline,
) -> PipelineOut:
    """Re-enter the current stage, bumping its iteration counter (a gate-failure
    rework re-entry). The persisted (stage, iteration) is what a restarted
    orchestrator resumes on."""
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)
        updated = await service.retry_pipeline_stage(
            conn, project_id, body, actor=str(principal.sub)
        )
    set_etag(response, updated.version)
    return updated


@router.post("/projects/{project_id}/pipeline:resume-budget", response_model=PipelineOut)
async def resume_pipeline_budget(
    request: Request,
    project_id: uuid.UUID,
    response: Response,
    principal: _OverridePipeline,
) -> PipelineOut:
    """Raise the project budget (clear the project hold) so a pipeline paused on
    budget hold resumes exactly where it paused (§ point 4)."""
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)
        updated = await service.resume_pipeline_budget(
            conn, project_id, actor=str(principal.sub), redis=request.app.state.redis
        )
    set_etag(response, updated.version)
    return updated


@router.post("/tasks/{task_id}:resume-budget", response_model=TaskOut)
async def resume_task_budget(
    request: Request, task_id: uuid.UUID, response: Response, principal: _Authenticated
) -> TaskOut:
    """Raise a task's budget (clear its task-scoped hold) so a paused task resumes
    exactly where it paused. Object-authorized on task:transition."""
    async with request.app.state.engine.begin() as conn:
        task = await service.get_task(conn, task_id)
        await _authorize_task(request, task, "task:transition")
        updated = await service.resume_task_budget(
            conn, task_id, actor=str(principal.sub), redis=request.app.state.redis
        )
    set_etag(response, updated.version)
    return updated


def mount(app: FastAPI) -> None:
    app.include_router(router)
