"""Clarification REST endpoints (Vol 6 §2.4). Private to the module; main.py
mounts it. The CEO submits a UserRequest and opens the batch; the user answers;
either party reads the current batch."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request

from titan.clarification import service
from titan.clarification.service import (
    AnswerSubmission,
    ClarificationBatchOut,
    UserRequestSubmission,
)
from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.projects import service as projects

router = APIRouter(prefix="/api/v1", tags=["clarification"])

_AdvancePipeline = Annotated[
    Principal, Depends(Requires("pipeline:advance", project_param="project_id"))
]
_ReadPipeline = Annotated[Principal, Depends(Requires("pipeline:read", project_param="project_id"))]


@router.post("/projects/{project_id}/clarifications", response_model=ClarificationBatchOut)
async def open_clarification(
    request: Request,
    project_id: uuid.UUID,
    principal: _AdvancePipeline,
    submission: UserRequestSubmission,
) -> ClarificationBatchOut:
    """Submit the UserRequest (H1) and open the clarification batch. A duplicate of
    an active project is 409 and an empty request is 400 (gate G1); otherwise ≤5
    essential questions are asked and every other unknown is logged as an
    assumption."""
    engine = request.app.state.engine
    async with engine.connect() as conn:
        await projects.get_project(conn, project_id)  # 404 before the FK insert
    return await service.submit_user_request(
        engine, project_id, submission, submitted_by=str(principal.sub)
    )


@router.post("/projects/{project_id}/clarifications/answers", response_model=ClarificationBatchOut)
async def answer_clarification(
    request: Request,
    project_id: uuid.UUID,
    principal: _AdvancePipeline,
    submission: AnswerSubmission,
) -> ClarificationBatchOut:
    """Record the user's answers; the batch closes once every question is answered."""
    return await service.answer_clarification(
        request.app.state.engine, project_id, submission, answered_by=str(principal.sub)
    )


@router.get("/projects/{project_id}/clarifications", response_model=ClarificationBatchOut)
async def get_clarification(
    request: Request, project_id: uuid.UUID, principal: _ReadPipeline
) -> ClarificationBatchOut:
    """The project's clarification batch with questions + logged assumptions."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.get_clarification(conn, project_id)


def mount(app: FastAPI) -> None:
    app.include_router(router)
