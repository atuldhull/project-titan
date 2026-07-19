"""Auth endpoints (§4.2): /auth/token, /auth/refresh, /auth/me, /auth/revoke,
/auth/api-keys — plus the strategy that fills the Phase-7 authentication slot.

Private to the identity module; main.py mounts via mount()."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import Requires, RequiresAuth, mark_public
from titan.identity import accounts, service
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


# --- Settings: self-service account surfaces (Vol 4 §6.13, Phase 64) ---------
#
# Authorised by BEING the principal, not by a permission string: every query
# below is filtered to `principal.sub`. There is deliberately no `user:*` scope
# — a permission that every authenticated caller would hold is not a permission.
#
# All of these are restricted to `kind == "user"`. An agent principal's `sub` is
# the human who minted its API key, so without this an agent token could list
# and REVOKE its owner's credentials and sessions. `create_api_key` above
# already draws exactly this line.


def _user_principal(request: Request) -> Principal:
    principal = current_principal(request)
    if principal.kind != "user":
        raise ProblemException(
            status=403,
            title="Forbidden",
            detail="Only user principals may manage their own account.",
            problem_type=problem_type("forbidden"),
        )
    return principal


def _not_found(detail: str) -> ProblemException:
    return ProblemException(
        status=404,
        title="Not found",
        detail=detail,
        problem_type=problem_type("not-found"),
    )


@router.get("/profile", response_model=accounts.ProfileOut, dependencies=[Depends(RequiresAuth())])
async def get_profile(request: Request) -> accounts.ProfileOut:
    principal = _user_principal(request)
    async with request.app.state.engine.connect() as conn:
        profile = await accounts.get_profile(conn, principal.sub)
    if profile is None:
        raise _not_found("This account no longer exists.")
    return profile


@router.patch(
    "/profile", response_model=accounts.ProfileOut, dependencies=[Depends(RequiresAuth())]
)
async def update_profile(request: Request, body: accounts.ProfileUpdate) -> accounts.ProfileOut:
    principal = _user_principal(request)
    async with request.app.state.engine.begin() as conn:
        profile = await accounts.update_profile(conn, principal.sub, body)
    if profile is None:
        raise _not_found("This account no longer exists.")
    return profile


@router.get(
    "/sessions", response_model=list[accounts.SessionOut], dependencies=[Depends(RequiresAuth())]
)
async def list_sessions(request: Request) -> list[accounts.SessionOut]:
    principal = _user_principal(request)
    async with request.app.state.engine.connect() as conn:
        return await accounts.list_sessions(
            conn, principal.sub, current_family_id=principal.family_id
        )


@router.delete("/sessions/{family_id}", status_code=204, dependencies=[Depends(RequiresAuth())])
async def revoke_session(request: Request, family_id: uuid.UUID) -> Response:
    """Sign one device out. Revoking the session you are currently using is
    allowed — it is "log out everywhere else", done one row at a time — so the
    UI, not the API, is what warns which one you are ending."""
    principal = _user_principal(request)
    revoked = await accounts.revoke_session(
        request.app.state.engine, request.app.state.redis, principal.sub, family_id
    )
    if not revoked:
        raise _not_found("No such session for this account.")
    return Response(status_code=204)


@router.get(
    "/api-keys", response_model=list[accounts.ApiKeyOut], dependencies=[Depends(RequiresAuth())]
)
async def list_api_keys(request: Request) -> list[accounts.ApiKeyOut]:
    principal = _user_principal(request)
    async with request.app.state.engine.connect() as conn:
        return await accounts.list_api_keys(conn, principal.sub)


@router.delete(
    "/api-keys/{key_id}",
    response_model=accounts.ApiKeyOut,
    dependencies=[Depends(RequiresAuth())],
)
async def revoke_api_key(request: Request, key_id: uuid.UUID) -> accounts.ApiKeyOut:
    principal = _user_principal(request)
    async with request.app.state.engine.begin() as conn:
        revoked = await accounts.revoke_api_key(conn, principal.sub, key_id)
    if revoked is None:
        raise _not_found("No such API key for this account.")
    return revoked


# --- Settings: member administration (Vol 4 §6.13) ---------------------------
#
# A separate router because these are org resources, not auth endpoints: the
# identity module owns the tables so the code belongs here, but mounting them
# under /api/v1/auth/... would misdescribe them.

members_router = APIRouter(prefix="/api/v1", tags=["members"])

_MemberReader = Annotated[Principal, Depends(Requires("member:read"))]
_MemberAdmin = Annotated[Principal, Depends(Requires("member:write"))]


@members_router.get("/members", response_model=list[accounts.MemberOut])
async def list_members(request: Request, principal: _MemberReader) -> list[accounts.MemberOut]:
    async with request.app.state.engine.connect() as conn:
        return await accounts.list_members(conn)


@members_router.get("/roles", response_model=list[str])
async def list_roles(request: Request, principal: _MemberReader) -> list[str]:
    """The role names the DB defines — §9.3's five, not §6.13's four. There is
    no `developer` role, so the picker reads this rather than offering one that
    assignment would reject."""
    async with request.app.state.engine.connect() as conn:
        return await accounts.list_roles(conn)


@members_router.put("/members/{user_id}/roles", response_model=accounts.MemberOut)
async def set_member_roles(
    request: Request,
    user_id: uuid.UUID,
    body: accounts.MemberRolesUpdate,
    principal: _MemberAdmin,
) -> accounts.MemberOut:
    async with request.app.state.engine.begin() as conn:
        try:
            member = await accounts.set_member_roles(conn, user_id, body)
        except accounts.LastOwnerError as exc:
            raise ProblemException(
                status=409,
                title="Last owner",
                detail=str(exc),
                problem_type=problem_type("conflict"),
            ) from exc
        except ValueError as exc:
            raise ProblemException(
                status=422,
                title="Unknown role",
                detail=str(exc),
                problem_type=problem_type("validation"),
            ) from exc
    if member is None:
        raise _not_found("No such member.")
    return member


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
    """Attach identity to the app: routers + keyring + auth slot strategy."""
    app.include_router(router)
    app.include_router(members_router)
    app.state.auth_strategy = auth_strategy
