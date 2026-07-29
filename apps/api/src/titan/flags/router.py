"""Feature-flag admin + read API (Vol 9 §5.3). Mounted by main.py via mount().

``GET /api/v1/flags`` is readable by any authenticated principal — the frontend
needs it and agents read flags through the orchestrator context — so it is
gated on the NON-sensitive ``flag:read`` scope, which every seeded role (and the
agent ``operator`` bundle) already holds via ``*:read``. Writes are gated on
``flag:write`` (Owner/Admin only), the same org-admin gate the LLM settings
router uses — flipping a kill switch is an operator act, never an agent's.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, FastAPI, Request, Response

from titan.flags.service import (
    FlagCreate,
    FlagExistsError,
    FlagOut,
    FlagProtectedError,
    FlagStore,
    FlagUpdate,
    flag_to_out,
)
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import Requires
from titan.identity.service import Principal

router = APIRouter(prefix="/api/v1", tags=["flags"])

_FlagReader = Annotated[Principal, Depends(Requires("flag:read"))]
_FlagAdmin = Annotated[Principal, Depends(Requires("flag:write"))]


def _store(request: Request) -> FlagStore:
    return cast(FlagStore, request.app.state.flags)


def _not_found(key: str) -> ProblemException:
    return ProblemException(
        status=404,
        title="Flag not found",
        detail=f"No feature flag with key '{key}'.",
        problem_type=problem_type("not-found"),
    )


@router.get("/flags", response_model=list[FlagOut])
async def list_flags(request: Request, principal: _FlagReader) -> list[FlagOut]:
    flags = await _store(request).list_flags()
    return [flag_to_out(f) for f in flags]


@router.get("/flags/{key}", response_model=FlagOut)
async def get_flag(request: Request, key: str, principal: _FlagReader) -> FlagOut:
    flag = await _store(request).get(key)
    if flag is None:
        raise _not_found(key)
    return flag_to_out(flag)


@router.post("/flags", response_model=FlagOut, status_code=201)
async def create_flag(request: Request, body: FlagCreate, principal: _FlagAdmin) -> FlagOut:
    try:
        flag = await _store(request).create(body)
    except FlagExistsError as exc:
        raise ProblemException(
            status=409,
            title="Flag already exists",
            detail=f"A feature flag with key '{body.key}' already exists.",
            problem_type=problem_type("flag-exists"),
        ) from exc
    return flag_to_out(flag)


@router.put("/flags/{key}", response_model=FlagOut)
async def update_flag(
    request: Request, key: str, body: FlagUpdate, principal: _FlagAdmin
) -> FlagOut:
    # exclude_unset: only the fields the client sent are applied, so a partial
    # flip (`{"enabled": true}`) leaves every other field untouched.
    flag = await _store(request).update(key, body.model_dump(exclude_unset=True))
    if flag is None:
        raise _not_found(key)
    return flag_to_out(flag)


@router.delete("/flags/{key}", status_code=204)
async def delete_flag(request: Request, key: str, principal: _FlagAdmin) -> Response:
    try:
        deleted = await _store(request).delete(key)
    except FlagProtectedError as exc:
        raise ProblemException(
            status=409,
            title="Flag is protected",
            detail="Ops kill switches are permanent platform controls and cannot be deleted.",
            problem_type=problem_type("flag-protected"),
        ) from exc
    if not deleted:
        raise _not_found(key)
    return Response(status_code=204)


def mount(app: FastAPI) -> None:
    app.include_router(router)
