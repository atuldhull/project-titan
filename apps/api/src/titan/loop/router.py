"""Self-improvement-loop REST endpoints (Vol 6 §3). Read-only: the loop record,
its cycles, findings, and the append-only stage-event audit trail (the WFL-011
message-log inspection surface). Private to the module; main.py mounts it.

Object-addressed (``/tasks/{task_id}/loop``): authenticate, load the task, then
authorize object-side against the task's own project (404 out-of-scope)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request

from titan.gateway.rbac import RequiresAuth, authorize_in_project
from titan.identity.service import Principal
from titan.loop import service
from titan.loop.service import LoopView
from titan.orchestrator import service as orchestrator

router = APIRouter(prefix="/api/v1", tags=["loop"])

_Authenticated = Annotated[Principal, Depends(RequiresAuth())]


@router.get("/tasks/{task_id}/loop", response_model=LoopView)
async def get_task_loop(
    request: Request, task_id: uuid.UUID, principal: _Authenticated
) -> LoopView:
    """The task's self-improvement loop with its cycles, findings, and the
    append-only stage-event audit trail."""
    async with request.app.state.engine.connect() as conn:
        task = await orchestrator.get_task(conn, task_id)  # 404 outside scope
        await authorize_in_project(request, task.project_id, "task:read", resource="task")
        return await service.get_loop(conn, task_id)


def mount(app: FastAPI) -> None:
    app.include_router(router)
