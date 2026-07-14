"""Derived-artifact REST + export endpoints (Vol 6 §5). Private to the module;
main.py attaches it via mount(). Regeneration needs ``pipeline:advance``; reads and
exports need ``pipeline:read``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request, Response

from titan.artifacts import service
from titan.artifacts.service import ArtifactSummary, DerivedArtifactOut
from titan.gateway.rbac import Requires
from titan.identity.service import Principal
from titan.projects import service as projects

router = APIRouter(prefix="/api/v1", tags=["artifacts"])

_Advance = Annotated[Principal, Depends(Requires("pipeline:advance", project_param="project_id"))]
_Read = Annotated[Principal, Depends(Requires("pipeline:read", project_param="project_id"))]


@router.post("/projects/{project_id}/artifacts:regenerate", response_model=list[ArtifactSummary])
async def regenerate(
    request: Request, project_id: uuid.UUID, principal: _Advance
) -> list[ArtifactSummary]:
    """Regenerate all six §5 artifacts from the current domain state (each versioned
    only if its content changed)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)  # 404 outside scope
    regenerated = await service.regenerate_all(request.app.state.engine, project_id)
    return [
        ArtifactSummary(
            kind=a.kind, artifact_version=a.artifact_version, content_hash=a.content_hash
        )
        for a in regenerated
    ]


@router.get("/projects/{project_id}/artifacts", response_model=list[ArtifactSummary])
async def list_artifacts(
    request: Request, project_id: uuid.UUID, principal: _Read
) -> list[ArtifactSummary]:
    """The current version summary of every generated artifact."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.list_artifacts(conn, project_id)


@router.get("/projects/{project_id}/artifacts/{kind}", response_model=DerivedArtifactOut)
async def get_artifact(
    request: Request, project_id: uuid.UUID, kind: str, principal: _Read
) -> DerivedArtifactOut:
    """The current version of one artifact (Markdown + JSON + hash)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.get_artifact(conn, project_id, kind)


@router.get("/projects/{project_id}/artifacts/{kind}/export")
async def export_artifact(
    request: Request, project_id: uuid.UUID, kind: str, principal: _Read, format: str = "md"
) -> Response:
    """Export one artifact as raw Markdown (``format=md``) or JSON (``format=json``)."""
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        artifact = await service.get_artifact(conn, project_id, kind)
    if format == "json":
        import json

        return Response(content=json.dumps(artifact.content_json), media_type="application/json")
    return Response(content=artifact.content_md, media_type="text/markdown")


def mount(app: FastAPI) -> None:
    app.include_router(router)
