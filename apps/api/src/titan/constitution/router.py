"""Constitution REST endpoints (Vol 6 §6). Read-only reference data every
authenticated member may retrieve — the standards agents are held to and the
version any gate/reviewer judged against. Private to the module; main.py mounts it."""

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request

from titan.constitution import service
from titan.constitution.service import ConstitutionHistory, ConstitutionOut
from titan.gateway.rbac import require_authenticated
from titan.identity.service import Principal

router = APIRouter(prefix="/api/v1", tags=["constitution"])

_Authed = Annotated[Principal, Depends(require_authenticated)]


@router.get("/constitution", response_model=ConstitutionOut)
async def get_active_constitution(request: Request, principal: _Authed) -> ConstitutionOut:
    """The active engineering-standards constitution (the version agents retrieve
    into context and gates judge against)."""
    async with request.app.state.engine.connect() as conn:
        return await service.active_constitution(conn)


@router.get("/constitution/versions", response_model=ConstitutionHistory)
async def list_constitution_versions(request: Request, principal: _Authed) -> ConstitutionHistory:
    """Every committed constitution version, newest first (audit history)."""
    async with request.app.state.engine.connect() as conn:
        return await service.constitution_history(conn)


@router.get("/constitution/{version}", response_model=ConstitutionOut)
async def get_constitution_version(
    request: Request, version: int, principal: _Authed
) -> ConstitutionOut:
    """A specific committed constitution version by number (404 if unknown)."""
    async with request.app.state.engine.connect() as conn:
        return await service.get_constitution(conn, version)


def mount(app: FastAPI) -> None:
    app.include_router(router)
