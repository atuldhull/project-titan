"""Technical-debt ledger REST endpoint (Vol 6 §5, TITAN-WFL-021). Read-only: the
open total + the tickets behind the project dashboard's debt widget. Private to the
module; main.py mounts it."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request

from titan.debt import service
from titan.debt.service import DebtLedger
from titan.gateway.rbac import RequiresAuth, authorize_in_project
from titan.identity.service import Principal
from titan.projects import service as projects

router = APIRouter(prefix="/api/v1", tags=["debt"])

_Authenticated = Annotated[Principal, Depends(RequiresAuth())]


@router.get("/projects/{project_id}/debt", response_model=DebtLedger)
async def get_debt_ledger(
    request: Request, project_id: uuid.UUID, principal: _Authenticated
) -> DebtLedger:
    """The project's technical-debt ledger: the open-ticket total + every ticket."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)  # 404 outside scope
        await authorize_in_project(request, project_id, "project:read", resource="project")
        return await service.ledger(conn, project_id)


def mount(app: FastAPI) -> None:
    app.include_router(router)
