"""Public service interface of the `identity` module (TITAN-BCK-004).

Responsibility: authentication (local mode now, OIDC in M7), JWT issuing/
verification, refresh rotation with family reuse detection, API keys.
Owns: users, roles, user_roles, local_credentials, refresh_tokens, api_keys.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""

import datetime as dt
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from typing import Literal

import structlog
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.db import uuid7
from titan.identity import mfa, passwords, tokens
from titan.identity.models import ApiKey, LocalCredential, RefreshToken, User
from titan.identity.tokens import ACCESS_TTL_SECONDS, KeyRing, TokenInvalid

logger = structlog.get_logger("titan.identity")

REFRESH_TTL = dt.timedelta(days=7)  # ≤ 14 d per TITAN-SEC-012
# Family-denylist TTL tracks the refresh lifetime so a revoked family's
# tokens can never outlive their denylist entry (audit #4).
REFRESH_DENY_TTL_SECONDS = int(REFRESH_TTL.total_seconds())
API_KEY_PREFIX = "titan_ak_"
REFRESH_PREFIX = "titan_rt_"


class AuthenticationFailed(Exception):
    """Bad credentials, expired/revoked/reused token — always maps to 401."""


class MfaEnrollmentRequired(AuthenticationFailed):
    """A privileged account under an mfa_required org must enroll before it can
    proceed (TITAN-SEC-015). Subclasses AuthenticationFailed so every existing
    ``except AuthenticationFailed`` on the refresh path blocks the bypass with a
    401 by default; callers that want the nicer ``mfa-enrollment-required``
    problem type can catch this more specific type first."""


class Principal(BaseModel):
    """The authenticated caller, attached to request.state.principal."""

    sub: uuid.UUID
    kind: Literal["user", "agent"]
    roles: list[str]
    scope: str
    jti: str
    family_id: str | None = None
    agent_id: uuid.UUID | None = None
    # Agent's bound project (TITAN-BCK-021: operator scoped to their project).
    # None until the agent→project binding lands with the agents table
    # (Phase 26); agents are fail-closed on all projects until then.
    project_id: uuid.UUID | None = None


class TokenPair(BaseModel):
    """Delivery shape for the token endpoints. The future Next.js BFF stores
    these in httpOnly/Secure/SameSite=Lax cookies (TITAN-SEC-013) — browser
    JS must never see them; this JSON body is for the BFF/CLI only."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_in: int = ACCESS_TTL_SECONDS


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _org_roles(conn: AsyncConnection, user_id: uuid.UUID) -> list[str]:
    rows = await conn.execute(
        text(
            "SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
            "WHERE ur.user_id = :uid AND ur.project_id IS NULL"
        ),
        {"uid": user_id},
    )
    return sorted(row[0] for row in rows)


@dataclass(frozen=True)
class AuthenticatedUser:
    """A verified-but-not-yet-minted user (Phase 67).

    `Principal` models a minted/verified token (it carries a `jti`), so it is
    the wrong carrier for the moment between "password checked" and "token
    issued" in the OAuth flow. This is that moment: identity and roles, no jti.
    """

    user_id: uuid.UUID
    roles: list[str]


async def authenticate_local(conn: AsyncConnection, email: str, password: str) -> AuthenticatedUser:
    """Verify a local password and return the user — mint NOTHING.

    The credential-check half of a login, split out in Phase 67 so the OAuth
    Authorization Code flow can authenticate at ``/oauth/authorize`` and defer
    token minting to the ``/oauth/token`` code exchange. `login_local` is now a
    thin wrapper that mints on top of this.

    Enumeration-resistant and rehash-aware (TITAN-SEC-017): a verify always runs
    (against a dummy hash for an unknown email, so timing does not leak
    existence), and a successful login with weaker-than-current parameters is
    transparently upgraded — the one moment the plaintext is in hand.
    """
    row = (
        await conn.execute(
            select(User.id, User.is_active, LocalCredential.password_hash)
            .join(LocalCredential, LocalCredential.user_id == User.id)
            .where(User.email == email)
        )
    ).first()

    stored_hash = row.password_hash if row is not None else passwords.DUMMY_HASH
    password_ok = passwords.verify_password(stored_hash, password)
    if row is None or not row.is_active or not password_ok:
        raise AuthenticationFailed("invalid credentials")

    if passwords.needs_rehash(stored_hash):
        await set_local_password(conn, row.id, password)

    roles = await _org_roles(conn, row.id)
    return AuthenticatedUser(user_id=row.id, roles=roles)


async def issue_refresh_for(conn: AsyncConnection, user_id: uuid.UUID) -> tuple[str, uuid.UUID]:
    """A fresh refresh token in a NEW rotation family, and that family's id.

    The OAuth token exchange calls this after redeeming a code, and MUST stamp
    the returned family id into the access token's `fam` claim — otherwise
    `revoke` (which kills the family named on the presented token) has nothing
    to revoke, and logout would not durably kill the session. A test caught
    exactly that regression.
    """
    family_id = uuid7()
    refresh = await _issue_refresh(conn, user_id, family_id)
    return refresh, family_id


async def login_local(
    conn: AsyncConnection, keyring: KeyRing, email: str, password: str
) -> TokenPair:
    """AUTH_MODE=local password login (ADR-010) — fully offline.

    Retained for the api-key/refresh surfaces and existing callers; user login
    proper now flows through the OAuth Authorization Code + PKCE path
    (`oauth_router`). This mints directly on top of `authenticate_local`.
    """
    user = await authenticate_local(conn, email, password)
    family_id = uuid7()
    refresh = await _issue_refresh(conn, user.user_id, family_id)
    access, _ = keyring.mint(
        sub=str(user.user_id), roles=user.roles, scope="user", family_id=str(family_id)
    )
    logger.info("auth.login", user_id=str(user.user_id))
    return TokenPair(access_token=access, refresh_token=refresh)


async def rotate_refresh(
    engine: AsyncEngine, redis: Redis, keyring: KeyRing, refresh_token: str
) -> TokenPair:
    """Single-use rotation with family reuse detection (TITAN-SEC-012).

    Owns its transactions: on reuse the family revocation MUST commit even
    though the request is rejected — a rollback would leave the replayed
    family usable. So this takes the engine, not a caller transaction.
    """
    async with engine.begin() as conn:
        # .one_or_none() (not .scalar_): a Core connection expands
        # select(Model) to all columns; scalar would return only id.
        row = (
            await conn.execute(
                select(RefreshToken).where(RefreshToken.token_hash == _sha256(refresh_token))
            )
        ).one_or_none()
        if row is None:
            raise AuthenticationFailed("unknown refresh token")

        now = dt.datetime.now(dt.UTC)
        # revoked_at also covers logout: revoke() writes it durably, so a
        # captured refresh token cannot rotate after logout (audit #1).
        if row.revoked_at is not None:
            raise AuthenticationFailed("refresh token revoked")
        if row.consumed_at is None and row.expires_at < now:
            raise AuthenticationFailed("refresh token expired")

        reuse = row.consumed_at is not None
        if not reuse:
            # Atomic single-use consume: WHERE consumed_at IS NULL + rowcount
            # closes the concurrent-double-submit race (audit #3). Losing the
            # race means a sibling already consumed it → treat as reuse.
            result = await conn.execute(
                update(RefreshToken)
                .where(RefreshToken.id == row.id, RefreshToken.consumed_at.is_(None))
                .values(consumed_at=now)
            )
            reuse = result.rowcount != 1

        if not reuse:
            roles = await _org_roles(conn, row.user_id)
            # Forced-MFA also gates REFRESH (TITAN-SEC-015). Without this a
            # privileged account that the org policy requires to enroll — but
            # hasn't — could keep a pre-policy session alive indefinitely by
            # rotating its refresh token, never passing the interactive
            # enrollment gate. This is the enrollment check, NOT a per-refresh
            # code prompt: it only fires for an owner/admin under an mfa_required
            # org with no confirmed credential, which is one small policy lookup
            # for everyone else. The token is left CONSUMED (rotation already
            # happened) so the presented token cannot be retried; the account
            # must re-login and enroll.
            required = await mfa.mfa_required_for_login(conn, row.user_id, keyring.org, roles)
            if required and not await mfa.is_mfa_enabled(conn, row.user_id):
                raise MfaEnrollmentRequired(
                    "MFA enrollment is required before this account can continue."
                )
            new_refresh = await _issue_refresh(conn, row.user_id, row.family_id)
            access, _ = keyring.mint(
                sub=str(row.user_id), roles=roles, scope="user", family_id=str(row.family_id)
            )
            return TokenPair(access_token=access, refresh_token=new_refresh)

        # REUSE: replayed or race-lost token. Kill the family in the DB and
        # COMMIT (this transaction succeeds); rejection is raised afterward.
        await conn.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == row.family_id)
            .values(revoked_at=now)
        )
        reused_family = row.family_id
        reused_user = row.user_id

    await tokens.revoke_family(redis, str(reused_family), REFRESH_DENY_TTL_SECONDS)
    logger.warning(
        "auth.refresh_reuse_detected",
        family_id=str(reused_family),
        user_id=str(reused_user),
    )
    raise AuthenticationFailed("refresh token reuse detected; family revoked")


async def revoke(engine: AsyncEngine, redis: Redis, principal: Principal) -> None:
    """Logout: durably revoke the refresh family (DB) + denylist jti/family
    in Redis (≤ 60 s access-token effect). The DB write is what makes a
    captured refresh token permanently unusable after logout (audit #1)."""
    await tokens.revoke_jti(redis, principal.jti)
    if principal.family_id:
        await tokens.revoke_family(redis, principal.family_id, REFRESH_DENY_TTL_SECONDS)
        async with engine.begin() as conn:
            await conn.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == uuid.UUID(principal.family_id))
                .values(revoked_at=dt.datetime.now(dt.UTC))
            )


async def verify_bearer(redis: Redis, keyring: KeyRing, token: str) -> Principal:
    """Static verification + revocation denylist (TITAN-SEC-011/014)."""
    claims = keyring.verify(token)
    if await tokens.is_revoked(redis, claims):
        raise AuthenticationFailed("token revoked")
    return Principal(
        sub=uuid.UUID(claims["sub"]),
        kind="agent" if claims.get("agent_id") else "user",
        roles=list(claims.get("roles", [])),
        scope=str(claims.get("scope", "")),
        jti=claims["jti"],
        family_id=claims.get("fam"),
        agent_id=uuid.UUID(claims["agent_id"]) if claims.get("agent_id") else None,
    )


async def create_api_key(
    conn: AsyncConnection,
    user_id: uuid.UUID,
    name: str,
    agent_id: uuid.UUID | None = None,
) -> tuple[str, uuid.UUID]:
    """Mint a titan_ak_… key; ONLY the SHA-256 is stored. Returns (key, id)."""
    raw = API_KEY_PREFIX + secrets.token_urlsafe(30)
    key_id = uuid7()
    await conn.execute(
        insert(ApiKey).values(
            id=key_id,
            user_id=user_id,
            agent_id=agent_id,
            name=name,
            prefix=raw[: len(API_KEY_PREFIX) + 8],
            key_hash=_sha256(raw),
        )
    )
    return raw, key_id


async def resolve_api_key(conn: AsyncConnection, api_key: str) -> Principal:
    """Authenticate an API key to a Principal WITHOUT minting a JWT — used
    by the per-request auth slot (audit #6: no throwaway RSA sign per call).
    Touches last_used_at at most hourly to avoid a write per request."""
    if not api_key.startswith(API_KEY_PREFIX):
        raise AuthenticationFailed("malformed api key")
    row = (
        await conn.execute(select(ApiKey).where(ApiKey.key_hash == _sha256(api_key)))
    ).one_or_none()
    now = dt.datetime.now(dt.UTC)
    if row is None or row.revoked_at is not None:
        raise AuthenticationFailed("unknown or revoked api key")
    if row.expires_at is not None and row.expires_at < now:
        raise AuthenticationFailed("api key expired")
    if row.last_used_at is None or (now - row.last_used_at) > dt.timedelta(hours=1):
        await conn.execute(update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now))
    return Principal(
        sub=row.user_id,
        kind="agent",
        roles=["operator"],
        scope="agent",
        jti=str(row.id),
        agent_id=row.agent_id or row.id,
    )


async def exchange_api_key(
    conn: AsyncConnection, keyring: KeyRing, api_key: str
) -> tuple[TokenPair, Principal]:
    """§9.2: API keys exchange for short-lived service JWTs whose agent_id
    claim makes every audit trail resolve to a principal. Agent service
    tokens carry the operator bundle (§9.3)."""
    if not api_key.startswith(API_KEY_PREFIX):
        raise AuthenticationFailed("malformed api key")
    row = (
        await conn.execute(select(ApiKey).where(ApiKey.key_hash == _sha256(api_key)))
    ).one_or_none()
    now = dt.datetime.now(dt.UTC)
    if row is None or row.revoked_at is not None:
        raise AuthenticationFailed("unknown or revoked api key")
    if row.expires_at is not None and row.expires_at < now:
        raise AuthenticationFailed("api key expired")

    await conn.execute(update(ApiKey).where(ApiKey.id == row.id).values(last_used_at=now))
    agent_claim = str(row.agent_id) if row.agent_id else str(row.id)
    access, jti = keyring.mint(
        sub=str(row.user_id), roles=["operator"], scope="agent", agent_id=agent_claim
    )
    principal = Principal(
        sub=row.user_id,
        kind="agent",
        roles=["operator"],
        scope="agent",
        jti=jti,
        agent_id=row.agent_id or row.id,
    )
    return TokenPair(access_token=access), principal


async def set_local_password(conn: AsyncConnection, user_id: uuid.UUID, password: str) -> None:
    """Upsert the Argon2id credential (seed + future settings screen)."""
    hashed = passwords.hash_password(password)
    await conn.execute(
        text(
            "INSERT INTO local_credentials (user_id, password_hash) "
            "VALUES (:uid, :hash) "
            "ON CONFLICT (user_id) DO UPDATE SET password_hash = :hash"
        ),
        {"uid": user_id, "hash": hashed},
    )


async def _issue_refresh(conn: AsyncConnection, user_id: uuid.UUID, family_id: uuid.UUID) -> str:
    raw = REFRESH_PREFIX + secrets.token_urlsafe(48)
    await conn.execute(
        insert(RefreshToken).values(
            id=uuid7(),
            family_id=family_id,
            user_id=user_id,
            token_hash=_sha256(raw),
            expires_at=dt.datetime.now(dt.UTC) + REFRESH_TTL,
        )
    )
    return raw


__all__ = [
    "API_KEY_PREFIX",
    "AuthenticationFailed",
    "KeyRing",
    "Principal",
    "TokenInvalid",
    "TokenPair",
    "create_api_key",
    "exchange_api_key",
    "login_local",
    "resolve_api_key",
    "revoke",
    "rotate_refresh",
    "set_local_password",
    "verify_bearer",
]
