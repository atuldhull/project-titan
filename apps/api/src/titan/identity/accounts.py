"""Account administration for Settings (Vol 4 §6.13, Phase 64).

Private to the `identity` package — `router.py` is the only importer, exactly as
`memory/explorer.py` sits beside `memory/router.py`. Everything here reads or
writes tables `identity` already owns (users, roles, user_roles, refresh_tokens,
api_keys); Phase 64 adds no identity table.

Three surfaces, and the distinction between them is a security boundary:

* **Self-service** (Profile, Sessions, API keys) is authorised by *being* the
  principal, not by a permission string. There is no `user:*` scope and adding
  one would be wrong: a permission that every authenticated caller holds is not
  a permission. The row filter (``user_id == principal.sub``) IS the check, so
  every function here takes the caller's id and scopes its own SQL by it.
* **Member administration** is `member:read` / `member:write` — carved into
  ``SENSITIVE_RESOURCES`` so `*:read` (which every agent carries) cannot
  enumerate the org's real people.
* **Sessions are refresh-token FAMILIES**, not rows. Rotation appends a row per
  refresh, so a single browser login is many rows sharing one ``family_id``;
  presenting the family as the session is what makes "revoke" mean "log that
  device out" rather than "invalidate one hop of the rotation chain".
"""

import datetime as dt
import uuid

import structlog
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.identity import tokens
from titan.identity.models import ApiKey, RefreshToken, Role, User, UserRole

logger = structlog.get_logger("titan.identity.accounts")

#: How long a revoked family stays on the Redis denylist. Mirrors
#: ``service.REFRESH_DENY_TTL_SECONDS``; duplicated rather than imported to keep
#: this module free of a cycle back through ``service``.
_REFRESH_DENY_TTL_SECONDS = int(dt.timedelta(days=7).total_seconds())


class ProfileOut(BaseModel):
    """The signed-in human. §6.13 also lists an avatar — there is no avatar
    column, no blob store and no upload path anywhere in TITAN, so this DTO
    does not pretend otherwise and the UI renders initials."""

    user_id: uuid.UUID
    email: str
    display_name: str
    auth_provider: str
    is_active: bool
    roles: list[str] = Field(default_factory=list)


class ProfileUpdate(BaseModel):
    """Only ``display_name`` is editable. Email is the local-auth login
    identity and the unique key; changing it is an account-recovery flow with
    a verification step, which does not exist — so it is read-only here rather
    than silently re-pointing a login.

    ``extra="forbid"`` is load-bearing, not tidiness: without it a submitted
    ``email`` is silently DROPPED and the caller gets a 200 with their old
    address, which reads exactly like a successful change that did not stick.
    Rejecting the field says so.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=200)


class SessionOut(BaseModel):
    """One refresh-token family = one signed-in device."""

    family_id: uuid.UUID
    created_at: dt.datetime
    last_used_at: dt.datetime
    expires_at: dt.datetime
    revoked: bool
    #: Past its refresh lifetime. An expired family cannot be used to obtain a
    #: token, so it is not a live session — but it is kept in the list rather
    #: than filtered out, because this list is a security audit surface and
    #: "where have I signed in" is a different question from "what is live now".
    #: The UI hides these; without the flag it showed dead logins as active for
    #: ever, since `revoked` alone never becomes true through mere expiry.
    expired: bool
    #: True for the family the calling token belongs to — the UI must not offer
    #: "revoke" for the session you are using without saying it logs you out.
    current: bool


class ApiKeyOut(BaseModel):
    """A personal access token. The secret is returned exactly once, at
    creation; this is the durable record and carries only the prefix."""

    id: uuid.UUID
    name: str
    prefix: str
    agent_id: uuid.UUID | None
    created_at: dt.datetime
    last_used_at: dt.datetime | None
    expires_at: dt.datetime | None
    revoked_at: dt.datetime | None


class RoleGrant(BaseModel):
    """One row of ``user_roles``. ``project_id`` NULL = org-wide."""

    role: str
    project_id: uuid.UUID | None


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    is_active: bool
    #: Org-wide role names (``project_id IS NULL``), sorted.
    roles: list[str] = Field(default_factory=list)
    #: Per-project grants, which the org roles do not imply.
    project_grants: list[RoleGrant] = Field(default_factory=list)


class MemberRolesUpdate(BaseModel):
    """Replace a member's grants at one scope. ``project_id`` NULL rewrites the
    org-wide set; a project id rewrites that project's set and leaves org-wide
    grants untouched."""

    model_config = ConfigDict(extra="forbid")

    roles: list[str] = Field(default_factory=list, max_length=16)
    project_id: uuid.UUID | None = None


class LastOwnerError(Exception):
    """Refusing to remove the final org-wide owner.

    Not a niceness: every guard resolves through role bundles, and `owner` is
    the only bundle holding ``*``. An org with no owner cannot grant `owner` to
    anyone — `member:write` lives in the owner and admin bundles, but the
    *seed* is what put it there, and nothing re-seeds a live database. The
    lockout is unrecoverable without direct SQL, so it is refused at the edge.
    """


# --- profile -----------------------------------------------------------------


async def get_profile(conn: AsyncConnection, user_id: uuid.UUID) -> ProfileOut | None:
    row = (
        await conn.execute(
            select(
                User.id, User.email, User.display_name, User.auth_provider, User.is_active
            ).where(User.id == user_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return ProfileOut(
        user_id=row.id,
        email=row.email,
        display_name=row.display_name,
        auth_provider=row.auth_provider,
        is_active=row.is_active,
        roles=await _org_roles(conn, user_id),
    )


async def update_profile(
    conn: AsyncConnection, user_id: uuid.UUID, patch: ProfileUpdate
) -> ProfileOut | None:
    result = await conn.execute(
        update(User).where(User.id == user_id).values(display_name=patch.display_name)
    )
    if result.rowcount == 0:
        return None
    return await get_profile(conn, user_id)


async def _org_roles(conn: AsyncConnection, user_id: uuid.UUID) -> list[str]:
    rows = await conn.execute(
        select(Role.name)
        .select_from(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user_id, UserRole.project_id.is_(None))
        .order_by(Role.name)
    )
    return [r.name for r in rows]


# --- sessions ----------------------------------------------------------------


async def list_sessions(
    conn: AsyncConnection, user_id: uuid.UUID, *, current_family_id: str | None
) -> list[SessionOut]:
    """One row per refresh-token FAMILY, newest first.

    A family is revoked if ANY of its rows is: reuse detection revokes the whole
    family, and `service.revoke` writes ``revoked_at`` across it, so
    ``max(revoked_at) IS NOT NULL`` is the family-level answer.

    (An earlier version of this docstring claimed ``min`` would answer
    differently. It would not — SQL aggregates SKIP NULLs, so ``min`` and
    ``max`` are both non-NULL exactly when some row is stamped. The reason to
    prefer ``max`` is that it reports WHEN the family finally died rather than
    when it first started to, which is the more useful timestamp; the liveness
    answer is the same either way.)
    """
    rows = await conn.execute(
        select(
            RefreshToken.family_id,
            func.min(RefreshToken.created_at).label("created_at"),
            func.max(RefreshToken.created_at).label("last_used_at"),
            func.max(RefreshToken.expires_at).label("expires_at"),
            func.max(RefreshToken.revoked_at).label("revoked_at"),
        )
        .where(RefreshToken.user_id == user_id)
        .group_by(RefreshToken.family_id)
        .order_by(func.max(RefreshToken.created_at).desc())
    )
    current = _parse_uuid(current_family_id)
    now = dt.datetime.now(dt.UTC)
    return [
        SessionOut(
            family_id=r.family_id,
            created_at=r.created_at,
            last_used_at=r.last_used_at,
            expires_at=r.expires_at,
            revoked=r.revoked_at is not None,
            expired=r.expires_at <= now,
            current=r.family_id == current,
        )
        for r in rows
    ]


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def revoke_session(
    engine: AsyncEngine,
    redis: Redis,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
) -> bool:
    """Revoke one of the caller's own sessions. False when the family is not
    theirs — the ownership filter is inside the UPDATE, so a forged family id
    belonging to another user changes nothing rather than 403-ing informatively.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            update(RefreshToken)
            .where(
                RefreshToken.family_id == family_id,
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=dt.datetime.now(dt.UTC))
        )
        if result.rowcount == 0:
            # Either not this user's family, or already revoked. Distinguish, so
            # a re-revoke is idempotent rather than a misleading 404.
            owned = (
                await conn.execute(
                    select(RefreshToken.family_id)
                    .where(
                        RefreshToken.family_id == family_id,
                        RefreshToken.user_id == user_id,
                    )
                    .limit(1)
                )
            ).one_or_none()
            if owned is None:
                return False
    # Denylist AFTER the durable write: the DB row is what makes a captured
    # refresh token permanently dead, and the Redis entry only shortens the
    # access-token window. Doing it in this order means a crash between the two
    # leaves the session revoked, not merely hidden.
    await tokens.revoke_family(redis, str(family_id), _REFRESH_DENY_TTL_SECONDS)
    return True


# --- API keys ----------------------------------------------------------------


async def list_api_keys(conn: AsyncConnection, user_id: uuid.UUID) -> list[ApiKeyOut]:
    rows = await conn.execute(
        select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
    )
    return [
        ApiKeyOut(
            id=r.id,
            name=r.name,
            prefix=r.prefix,
            agent_id=r.agent_id,
            created_at=r.created_at,
            last_used_at=r.last_used_at,
            expires_at=r.expires_at,
            revoked_at=r.revoked_at,
        )
        for r in rows
    ]


async def revoke_api_key(
    conn: AsyncConnection, user_id: uuid.UUID, key_id: uuid.UUID
) -> ApiKeyOut | None:
    """Stamp ``revoked_at``. Both auth paths (`resolve_api_key` and
    `exchange_api_key`) already reject a key with a non-NULL ``revoked_at``, so
    this takes effect on the very next request — the column had no writer until
    now, which is why a minted key was previously immortal."""
    now = dt.datetime.now(dt.UTC)
    result = await conn.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    if result.rowcount == 0:
        # Already revoked, or not the caller's key. Return the row when it is
        # theirs so a double-revoke is idempotent.
        existing = (
            await conn.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id))
        ).one_or_none()
        if existing is None:
            return None
    rows = await list_api_keys(conn, user_id)
    return next((k for k in rows if k.id == key_id), None)


# --- members -----------------------------------------------------------------


async def list_members(conn: AsyncConnection) -> list[MemberOut]:
    """Every user with their grants. TITAN is single-org by construction — there
    is no organizations table — so "members" is simply "users"."""
    users = (
        await conn.execute(
            select(User.id, User.email, User.display_name, User.is_active).order_by(
                User.display_name, User.email
            )
        )
    ).all()
    grants = (
        await conn.execute(
            select(UserRole.user_id, Role.name, UserRole.project_id)
            .select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .order_by(Role.name)
        )
    ).all()

    org: dict[uuid.UUID, list[str]] = {}
    per_project: dict[uuid.UUID, list[RoleGrant]] = {}
    for g in grants:
        if g.project_id is None:
            org.setdefault(g.user_id, []).append(g.name)
        else:
            per_project.setdefault(g.user_id, []).append(
                RoleGrant(role=g.name, project_id=g.project_id)
            )
    return [
        MemberOut(
            user_id=u.id,
            email=u.email,
            display_name=u.display_name,
            is_active=u.is_active,
            roles=sorted(org.get(u.id, [])),
            project_grants=per_project.get(u.id, []),
        )
        for u in users
    ]


async def list_roles(conn: AsyncConnection) -> list[str]:
    """The role names the DB actually defines.

    Vol 4 §6.13 names owner/admin/developer/viewer. There is no `developer`
    role in TITAN — §9.3's five are owner, admin, maintainer, operator and
    viewer — so this reads the table rather than hard-coding the spec's list
    and offering a role that assignment would reject.
    """
    rows = await conn.execute(select(Role.name).order_by(Role.name))
    return [r.name for r in rows]


async def set_member_roles(
    conn: AsyncConnection, user_id: uuid.UUID, patch: MemberRolesUpdate
) -> MemberOut | None:
    """Replace a member's grants at one scope (org-wide, or one project).

    Raises :class:`LastOwnerError` if the write would leave the org with no
    org-wide owner.
    """
    exists = (await conn.execute(select(User.id).where(User.id == user_id))).one_or_none()
    if exists is None:
        return None

    known = {r.name: r.id for r in await conn.execute(select(Role.id, Role.name))}
    unknown = sorted(set(patch.roles) - set(known))
    if unknown:
        raise ValueError(f"unknown role(s): {', '.join(unknown)}")

    if patch.project_id is not None:
        # `user_roles.project_id` has a real FK, so an unknown id would surface
        # as an IntegrityError from the INSERT — an unhandled 500 for what is
        # plainly a bad request. Check it here so the router's existing
        # ValueError → 422 mapping applies.
        project = (
            await conn.execute(
                text("SELECT 1 FROM projects WHERE id = :pid"), {"pid": patch.project_id}
            )
        ).one_or_none()
        if project is None:
            raise ValueError(f"unknown project '{patch.project_id}'")

    if patch.project_id is None and not await _keeps_an_owner(conn, user_id, patch.roles):
        raise LastOwnerError("the last org-wide owner cannot be demoted")

    await conn.execute(
        delete(UserRole).where(
            UserRole.user_id == user_id,
            UserRole.project_id.is_(None)
            if patch.project_id is None
            else UserRole.project_id == patch.project_id,
        )
    )
    for name in sorted(set(patch.roles)):
        await conn.execute(
            insert(UserRole).values(
                user_id=user_id, role_id=known[name], project_id=patch.project_id
            )
        )
    members = await list_members(conn)
    return next((m for m in members if m.user_id == user_id), None)


async def _keeps_an_owner(conn: AsyncConnection, user_id: uuid.UUID, next_roles: list[str]) -> bool:
    """True if the org still has an org-wide owner after this write."""
    if "owner" in next_roles:
        return True
    others = (
        await conn.execute(
            select(func.count())
            .select_from(UserRole)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                Role.name == "owner",
                UserRole.project_id.is_(None),
                UserRole.user_id != user_id,
            )
        )
    ).scalar_one()
    return int(others) > 0


__all__ = [
    "ApiKeyOut",
    "LastOwnerError",
    "MemberOut",
    "MemberRolesUpdate",
    "ProfileOut",
    "ProfileUpdate",
    "RoleGrant",
    "SessionOut",
    "get_profile",
    "list_api_keys",
    "list_members",
    "list_roles",
    "list_sessions",
    "revoke_api_key",
    "revoke_session",
    "set_member_roles",
    "update_profile",
]
