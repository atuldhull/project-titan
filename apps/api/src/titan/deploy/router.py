"""Deployment REST endpoints (Vol 7 §6.7). Read-only: the deployment records for
a project and a single deployment by id — the data behind the dashboard's
Deployment view (its status, reachable URL, and health). Private to the module;
main.py mounts it.

The project-scoped list authorizes against the path project; the by-id read is
object-addressed — load the record, then authorize against its own project so an
out-of-scope id 404s without leaking existence (TITAN-SEC-032)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request

from titan.deploy import service
from titan.deploy.service import DeploymentOut
from titan.gateway.rbac import RequiresAuth, authorize_in_project
from titan.identity.service import Principal
from titan.projects import service as projects

router = APIRouter(prefix="/api/v1", tags=["deployments"])

_Authenticated = Annotated[Principal, Depends(RequiresAuth())]


@router.get("/projects/{project_id}/deployments", response_model=list[DeploymentOut])
async def list_project_deployments(
    request: Request, project_id: uuid.UUID, principal: _Authenticated
) -> list[DeploymentOut]:
    """Every local deployment recorded for the project, newest first."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)  # 404 outside scope
        await authorize_in_project(request, project_id, "project:read", resource="project")
        return await service.list_deployments(conn, project_id)


@router.get("/deployments/{deployment_id}", response_model=DeploymentOut)
async def get_deployment(
    request: Request, deployment_id: uuid.UUID, principal: _Authenticated
) -> DeploymentOut:
    """A single deployment record with its status, reachable URL, and health."""
    async with request.app.state.engine.connect() as conn:
        deployment = await service.get_deployment(conn, deployment_id)
        await authorize_in_project(
            request, deployment.project_id, "project:read", resource="deployment"
        )
        return deployment


def mount(app: FastAPI) -> None:
    app.include_router(router)
