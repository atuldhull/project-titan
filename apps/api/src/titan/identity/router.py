"""Auth endpoints (§4.2): /auth/token, /auth/refresh, /auth/me, /auth/revoke,
/auth/api-keys — plus the strategy that fills the Phase-7 authentication slot.

Private to the identity module; main.py mounts via mount()."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import RequiresAuth, mark_public
from titan.identity import service
from titan.identity.service import AuthenticationFailed, Principal, TokenInvalid, TokenPair

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class PasswordGrant(BaseModel):
    grant_type: Literal["password"] = "password"
    # Plain str, not EmailStr: local mode uses .local addresses (reserved
    # TLD) and the credential match — not format validation — is the auth
    # boundary. Login must not leak which addresses are well-formed.
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1)


class ApiKeyGrant(BaseModel):
    grant_type: Literal["api_key"]
    api_key: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class ApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agent_id: uuid.UUID | None = None


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    # Returned exactly once; only the SHA-256 is stored (§9.2).
    api_key: str


class MeResponse(BaseModel):
    sub: uuid.UUID
    kind: Literal["user", "agent"]
    roles: list[str]
    scope: str
    agent_id: uuid.UUID | None = None


def _unauthorized(detail: str) -> ProblemException:
    return ProblemException(
        status=401,
        title="Authentication failed",
        detail=detail,
        problem_type=problem_type("unauthorized"),
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_principal(request: Request) -> Principal:
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise _unauthorized("This endpoint requires authentication.")
    return principal


@router.post("/token", response_model=TokenPair)
@mark_public  # pre-auth: minting tokens cannot require an existing token
async def token(request: Request, grant: PasswordGrant | ApiKeyGrant) -> TokenPair:
    app = request.app
    async with app.state.engine.begin() as conn:
        try:
            if isinstance(grant, PasswordGrant):
                return await service.login_local(
                    conn, app.state.keyring, grant.email, grant.password
                )
            pair, _ = await service.exchange_api_key(conn, app.state.keyring, grant.api_key)
            return pair
        except AuthenticationFailed as exc:
            raise _unauthorized(str(exc)) from exc


@router.post("/refresh", response_model=TokenPair)
@mark_public  # pre-auth: the access token has expired, that's why we refresh
async def refresh(request: Request, body: RefreshRequest) -> TokenPair:
    app = request.app
    try:
        # rotate_refresh owns its transactions (family revocation must commit
        # even when the request is rejected).
        return await service.rotate_refresh(
            app.state.engine, app.state.redis, app.state.keyring, body.refresh_token
        )
    except AuthenticationFailed as exc:
        raise _unauthorized(str(exc)) from exc


@router.get("/me", response_model=MeResponse, dependencies=[Depends(RequiresAuth())])
async def me(request: Request) -> MeResponse:
    principal = current_principal(request)
    return MeResponse(
        sub=principal.sub,
        kind=principal.kind,
        roles=principal.roles,
        scope=principal.scope,
        agent_id=principal.agent_id,
    )


@router.post("/revoke", status_code=204, dependencies=[Depends(RequiresAuth())])
async def revoke(request: Request) -> Response:
    """Logout: durably revokes the caller's refresh family + denylists jti."""
    principal = current_principal(request)
    await service.revoke(request.app.state.engine, request.app.state.redis, principal)
    return Response(status_code=204)


@router.post(
    "/api-keys",
    response_model=ApiKeyResponse,
    status_code=201,
    dependencies=[Depends(RequiresAuth())],
)
async def create_api_key(request: Request, body: ApiKeyRequest) -> ApiKeyResponse:
    principal = current_principal(request)
    if principal.kind != "user":
        raise ProblemException(
            status=403,
            title="Forbidden",
            detail="Only user principals may mint API keys.",
            problem_type=problem_type("forbidden"),
        )
    async with request.app.state.engine.begin() as conn:
        raw, key_id = await service.create_api_key(conn, principal.sub, body.name, body.agent_id)
    return ApiKeyResponse(id=key_id, api_key=raw)


async def auth_strategy(request: Request) -> Response | None:
    """Fills the Phase-7 AuthenticationSlot (app.state.auth_strategy).

    Resolves credentials to request.state.principal when present and valid.
    A missing OR present-but-invalid credential leaves principal=None and
    PASSES THROUGH (audit #2): the endpoint / Phase-9 RBAC decides — so a
    stale token attached to /auth/refresh, /auth/token, or /healthz does not
    wrongly 401. request.state.auth_error records why, for downstream 401s.
    """
    request.state.principal = None
    request.state.auth_error = None
    app = request.app

    authorization = request.headers.get("Authorization")
    api_key = request.headers.get("X-API-Key")

    try:
        if authorization and authorization.startswith("Bearer "):
            request.state.principal = await service.verify_bearer(
                app.state.redis, app.state.keyring, authorization.removeprefix("Bearer ")
            )
        elif api_key:
            async with app.state.engine.begin() as conn:
                request.state.principal = await service.resolve_api_key(conn, api_key)
    except (AuthenticationFailed, TokenInvalid) as exc:
        request.state.auth_error = str(exc)
    return None


def mount(app: FastAPI) -> None:
    """Attach identity to the app: router + keyring + auth slot strategy."""
    app.include_router(router)
    app.state.auth_strategy = auth_strategy
