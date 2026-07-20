"""OAuth2 / OIDC endpoints for the embedded IdP (Phase 67; TITAN-SEC-010/015).

The first-party Authorization Code + PKCE front door that
`docs/authentication-grants.md` describes, plus the MFA management surface.

- ``POST /api/v1/oauth/authorize`` — the first-party login. Takes credentials
  PLUS the PKCE challenge and redirect_uri, authenticates (with the same
  per-account/per-IP throttling and MFA enforcement as any login), and returns
  a one-time authorization code bound to the challenge. It receives credentials
  because the embedded IdP OWNS the credential — the password reaches the
  authorization server and what comes back is a redirect carrying a code, never
  a token. That is what distinguishes it from the forbidden password grant.
- ``POST /api/v1/oauth/token`` — exchanges ``authorization_code`` + verifier
  for TITAN's own RS256 tokens, or rotates a ``refresh_token``.
  ``grant_type=password`` and ``implicit`` are rejected with OAuth errors.
- ``GET /api/v1/oauth/jwks`` and ``GET /.well-known/openid-configuration``.
- ``POST /api/v1/auth/mfa/enroll`` · ``/mfa/confirm`` · ``GET/PUT
  /api/v1/auth/mfa/policy`` — authenticated MFA enrollment and the org policy.

The Phase-8 ``/api/v1/auth/token`` keeps the ``api_key`` grant (a service
credential) but now REJECTS ``grant_type=password`` with an OAuth error — user
login flows entirely through the authorize path (see router.py).
"""

from __future__ import annotations

import math
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from titan.gateway.client_ip import resolve_client_ip
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import Requires, RequiresAuth, mark_public
from titan.identity import audit, mfa, oidc, service
from titan.identity.oidc import AuthCodePayload, OAuthError
from titan.identity.service import (
    AuthenticationFailed,
    MfaEnrollmentRequired,
    Principal,
    TokenPair,
)
from titan.identity.throttle import LoginThrottle
from titan.identity.tokens import KeyRing

oauth_router = APIRouter(tags=["oauth"])
mfa_router = APIRouter(prefix="/api/v1/auth/mfa", tags=["mfa"])

# The org policy is owner/admin-only; both roles hold `settings:write` (§4.2).
_SettingsWriter = Annotated[Principal, Depends(Requires("settings:write"))]
_SettingsReader = Annotated[Principal, Depends(Requires("settings:read"))]


def _require_user(
    principal: Annotated[Principal, Depends(RequiresAuth())],
) -> Principal:
    """Authenticated AND a human (kind == "user"), for MFA self-management.

    An API-key/agent principal's `sub` is the HUMAN who minted the key
    (`resolve_api_key` sets sub=owner), so bare `RequiresAuth` would let a
    low-trust automation credential enroll a second factor IT controls on its
    owner's account and harvest the recovery codes — an account-takeover /
    lockout path. router.py draws exactly this line for every account
    self-service route (`_user_principal`); the MFA routes must too. An agent
    must never mutate the owner human's authentication factors.

    Depends on `RequiresAuth()` (not a bare request read) so the route-guard
    gate still finds a titan guard in the dependency tree.
    """
    if principal.kind != "user":
        raise ProblemException(
            status=403,
            title="Forbidden",
            detail="Only user principals may manage their own MFA.",
            problem_type=problem_type("forbidden"),
        )
    return principal


# Enroll/confirm act on the CALLER's own account — human-only (see above).
_AnyUser = Annotated[Principal, Depends(_require_user)]

# A TOTP code is replayable within its validity window unless the acceptance is
# remembered. mfa.verify_totp cannot (its table has no last-timestep column), so
# the replay guard lives here in Redis: the first acceptance of a given code for
# a user claims a short-lived key; a second presentation inside the window is
# refused. Recovery codes are already single-use in the DB, so this only needs
# to cover the TOTP path — but keying on the raw code covers both harmlessly.
_MFA_REPLAY_TTL_SECONDS = 90


# --- request/response models ------------------------------------------------


class AuthorizeRequest(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1)
    totp_code: str | None = Field(default=None, max_length=64)
    response_type: str = "code"
    redirect_uri: str = Field(min_length=1)
    code_challenge: str = Field(min_length=1)
    code_challenge_method: str = "S256"
    scope: str = "user"
    state: str | None = None


class AuthorizeResponse(BaseModel):
    """The redirect the BFF follows, returned as JSON so the server-side BFF can
    read `code`/`state` directly rather than intercepting a 302."""

    redirect_uri: str
    code: str
    state: str | None = None


class TokenExchangeRequest(BaseModel):
    grant_type: str
    code: str | None = None
    code_verifier: str | None = None
    redirect_uri: str | None = None
    refresh_token: str | None = None


class EnrollResponse(BaseModel):
    secret: str
    provisioning_uri: str
    qr_png_data_url: str


class ConfirmRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class ConfirmResponse(BaseModel):
    recovery_codes: list[str]


class MfaPolicy(BaseModel):
    mfa_required: bool


# --- error helpers ----------------------------------------------------------


def _throttled(retry_after_seconds: float) -> ProblemException:
    retry_after = max(1, math.ceil(retry_after_seconds))
    return ProblemException(
        status=429,
        title="Too many attempts",
        detail="Too many failed sign-in attempts. Try again shortly.",
        problem_type=problem_type("rate-limited"),
        headers={"Retry-After": str(retry_after)},
    )


def _unauthorized(detail: str) -> ProblemException:
    return ProblemException(
        status=401,
        title="Authentication failed",
        detail=detail,
        problem_type=problem_type("unauthorized"),
        headers={"WWW-Authenticate": "Bearer"},
    )


def _mfa_required(*, enrollment_required: bool) -> ProblemException:
    """401 telling the caller a second factor is needed.

    Two distinct problem types rather than an extra body field, so the BFF
    branches on the type URI: `mfa-enrollment-required` means the account must
    enroll first (forced enrollment, TITAN-SEC-015); `mfa-required` means it has
    MFA and must supply a code.
    """
    slug = "mfa-enrollment-required" if enrollment_required else "mfa-required"
    return ProblemException(
        status=401,
        title="MFA required",
        detail=(
            "This account must enroll a second factor before signing in."
            if enrollment_required
            else "A second authentication factor is required."
        ),
        problem_type=problem_type(slug),
        headers={"WWW-Authenticate": "Bearer"},
    )


# --- OAuth endpoints --------------------------------------------------------


@oauth_router.post("/api/v1/oauth/authorize", response_model=AuthorizeResponse)
@mark_public  # pre-auth: this is where a user becomes authenticated
async def authorize(request: Request, body: AuthorizeRequest) -> AuthorizeResponse:
    """First-party login → one-time authorization code (TITAN-SEC-010)."""
    app = request.app
    # An implicit-flow attempt (`response_type=token`) is rejected before any
    # credential is touched — the OAuthError handler renders the OAuth body.
    oidc.validate_response_type(body.response_type)

    source_ip = resolve_client_ip(request, app.state.settings.trusted_proxies)
    throttle = _throttle(app)

    decision = await throttle.check(body.email, source_ip)
    if not decision.allowed:
        await audit.record(
            app.state.engine,
            audit.SecurityEventType.LOGIN_THROTTLED,
            email=body.email,
            source_ip=source_ip,
            detail={
                "triggered_by": decision.triggered_by,
                "retry_after": round(decision.retry_after_seconds, 3),
            },
        )
        raise _throttled(decision.retry_after_seconds)

    # 1) Password.
    try:
        async with app.state.engine.begin() as conn:
            user = await service.authenticate_local(conn, body.email, body.password)
    except AuthenticationFailed as exc:
        await throttle.record_failure(body.email, source_ip)
        await audit.record(
            app.state.engine,
            audit.SecurityEventType.LOGIN_FAILED,
            email=body.email,
            source_ip=source_ip,
        )
        raise _unauthorized(str(exc)) from exc

    # 2) Second factor, when the account has one or the org policy demands it.
    async with app.state.engine.begin() as conn:
        enrolled = await mfa.is_mfa_enabled(conn, user.user_id)
        policy_requires = await mfa.mfa_required_for_login(
            conn, user.user_id, app.state.settings.org, user.roles
        )

    if policy_requires and not enrolled:
        # Password correct, but a privileged account under policy must enroll
        # before it can finish a login. Throttle NOT cleared — login incomplete.
        await audit.record(
            app.state.engine,
            audit.SecurityEventType.MFA_REQUIRED_BLOCKED,
            user_id=user.user_id,
            email=body.email,
            source_ip=source_ip,
            detail={"reason": "enrollment_required"},
        )
        raise _mfa_required(enrollment_required=True)

    if enrolled:
        if body.totp_code is None:
            raise _mfa_required(enrollment_required=False)
        await _guard_totp_replay(app, user.user_id, body.totp_code)
        async with app.state.engine.begin() as conn:
            ok = await mfa.verify_totp(conn, user.user_id, body.totp_code)
        if not ok:
            await throttle.record_failure(body.email, source_ip)
            raise _unauthorized("Invalid authentication code.")

    # 3) Success. Bind an authorization code to the PKCE challenge.
    await throttle.clear(body.email, source_ip)
    await audit.record(
        app.state.engine,
        audit.SecurityEventType.LOGIN_SUCCEEDED,
        user_id=user.user_id,
        email=body.email,
        source_ip=source_ip,
    )
    code = await oidc.AuthCodeStore(app.state.redis).issue(
        AuthCodePayload(
            user_id=str(user.user_id),
            redirect_uri=body.redirect_uri,
            code_challenge=body.code_challenge,
            code_challenge_method=body.code_challenge_method,
            scope=body.scope,
            roles=user.roles,
        )
    )
    return AuthorizeResponse(redirect_uri=body.redirect_uri, code=code, state=body.state)


@oauth_router.post("/api/v1/oauth/token", response_model=TokenPair)
@mark_public  # pre-auth: this mints the first tokens
async def oauth_token(request: Request, body: TokenExchangeRequest) -> TokenPair:
    """Authorization-code exchange, or refresh. Every other grant is rejected."""
    app = request.app
    # `password`/`implicit`/`client_credentials` raise OAuthError here — the
    # machine-checkable half of TITAN-SEC-010.
    oidc.validate_grant_type(body.grant_type)

    if body.grant_type == "refresh_token":
        if not body.refresh_token:
            raise OAuthError("invalid_request", "refresh_token is required")
        try:
            return await service.rotate_refresh(
                app.state.engine, app.state.redis, app.state.keyring, body.refresh_token
            )
        except MfaEnrollmentRequired as exc:
            # Forced-MFA gates refresh too (TITAN-SEC-015): a privileged
            # unenrolled account cannot rotate past the enrollment gate.
            raise _mfa_required(enrollment_required=True) from exc
        except AuthenticationFailed as exc:
            raise OAuthError("invalid_grant", str(exc)) from exc

    if not (body.code and body.code_verifier and body.redirect_uri):
        raise OAuthError(
            "invalid_request", "code, code_verifier and redirect_uri are required"
        )
    payload = await oidc.AuthCodeStore(app.state.redis).consume(
        body.code, body.redirect_uri, body.code_verifier
    )
    async with app.state.engine.begin() as conn:
        refresh, family_id = await service.issue_refresh_for(
            conn, uuid.UUID(payload.user_id)
        )
    # The `fam` claim must match the refresh family, or `revoke`/logout cannot
    # kill this session (TITAN-SEC-012/014).
    access, _jti = app.state.keyring.mint(
        sub=payload.user_id,
        roles=payload.roles,
        scope=payload.scope,
        family_id=str(family_id),
    )
    return TokenPair(access_token=access, refresh_token=refresh)


@oauth_router.get("/api/v1/oauth/jwks")
@mark_public
async def jwks(request: Request) -> dict[str, object]:
    """The signing public key set (RFC 7517; TITAN-SEC-011)."""
    keyring: KeyRing = request.app.state.keyring
    return keyring.public_jwks()


@oauth_router.get("/.well-known/openid-configuration")
@mark_public
async def openid_configuration(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    base_url = str(request.base_url).rstrip("/")
    return oidc.discovery_document(issuer=settings.jwt_issuer, base_url=base_url)


# --- MFA management ---------------------------------------------------------


@mfa_router.post("/enroll", response_model=EnrollResponse)
async def enroll(request: Request, principal: _AnyUser) -> EnrollResponse:
    """Begin TOTP enrollment for the CALLER's own account."""
    app = request.app
    try:
        async with app.state.engine.begin() as conn:
            result = await mfa.begin_enrollment(
                conn,
                principal.sub,
                account_label=str(principal.sub),
                issuer=app.state.settings.jwt_issuer,
            )
    except mfa.MfaAlreadyEnrolled as exc:
        raise ProblemException(
            status=409,
            title="Already enrolled",
            detail=str(exc),
            problem_type=problem_type("conflict"),
        ) from exc
    return EnrollResponse(**result.model_dump())


@mfa_router.post("/confirm", response_model=ConfirmResponse)
async def confirm(
    request: Request, body: ConfirmRequest, principal: _AnyUser
) -> ConfirmResponse:
    """Confirm enrollment with a code; returns the one-time recovery codes."""
    app = request.app
    try:
        async with app.state.engine.begin() as conn:
            codes = await mfa.confirm_enrollment(conn, principal.sub, body.code)
    except mfa.MfaNotEnrolled as exc:
        raise ProblemException(
            status=409,
            title="No pending enrollment",
            detail=str(exc),
            problem_type=problem_type("conflict"),
        ) from exc
    except mfa.MfaCodeInvalid as exc:
        raise _unauthorized(str(exc)) from exc
    return ConfirmResponse(recovery_codes=codes)


@mfa_router.get("/policy", response_model=MfaPolicy)
async def get_policy(request: Request, principal: _SettingsReader) -> MfaPolicy:
    app = request.app
    async with app.state.engine.connect() as conn:
        required = await mfa.get_org_mfa_required(conn, app.state.settings.org)
    return MfaPolicy(mfa_required=required)


@mfa_router.put("/policy", response_model=MfaPolicy)
async def set_policy(
    request: Request, body: MfaPolicy, principal: _SettingsWriter
) -> MfaPolicy:
    app = request.app
    async with app.state.engine.begin() as conn:
        await mfa.set_org_mfa_required(conn, app.state.settings.org, body.mfa_required)
    return body


# --- internals --------------------------------------------------------------


def _throttle(app: FastAPI) -> LoginThrottle:
    return LoginThrottle(app.state.redis)


async def _guard_totp_replay(app: FastAPI, user_id: uuid.UUID, code: str) -> None:
    """Refuse a code already accepted for this user inside the TOTP window.

    A Redis SETNX claims `mfa:used:{user}:{code}`; if the key already exists the
    same code is being presented twice, which for a 30s TOTP is a replay. Fails
    open on a Redis error (consistent with the throttle) — the DB still makes
    recovery codes single-use regardless.
    """
    key = f"mfa:used:{user_id}:{code}"
    try:
        claimed = await app.state.redis.set(key, "1", nx=True, ex=_MFA_REPLAY_TTL_SECONDS)
    except Exception:  # pragma: no cover - fail-open
        return
    if not claimed:
        raise _unauthorized("Invalid authentication code.")


def mount_oauth(app: FastAPI) -> None:
    """Attach the OAuth + MFA routers and the OAuthError → RFC 6749 handler.

    OAuth errors need the ``{error, error_description}`` body an OAuth client
    parses, not problem+json — so `OAuthError` gets its own handler here rather
    than being mapped onto a ProblemException. All four validate/consume paths
    just `raise OAuthError(...)` and this renders it once, as a 400.
    """
    app.include_router(oauth_router)
    app.include_router(mfa_router)

    @app.exception_handler(OAuthError)
    async def _on_oauth_error(_request: Request, exc: OAuthError) -> JSONResponse:
        return JSONResponse(status_code=400, content=exc.to_dict())
