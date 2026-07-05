"""RBAC engine + the single authorization enforcement point (§9.3, §3.4).

Authorization is enforced by ONE mechanism: the :class:`Requires` FastAPI
dependency (spec §3.4 "one enforcement point: a FastAPI dependency"). Every
non-public route declares ``Requires("resource:action")``; the route-guard
lint (scripts/check_route_guards.py) fails CI if any route omits it
(TITAN-BCK-021 / TITAN-SEC-030).

Permission strings are ``resource:action``. A required ``r:a`` is granted if
the principal's permission set contains any of ``*``, ``r:*``, ``*:a``, or
``r:a`` (roles are additive; there are no deny rules — §4.1).

Object-level authorization (no IDOR, TITAN-SEC-032): resources outside the
caller's scope must 404, never 403 — use :func:`not_found` so existence is
never leaked.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.gateway.problems import ProblemException, problem_type
from titan.identity.service import Principal

logger = structlog.get_logger("titan.gateway.rbac")

# Machine principals (agent service JWTs) carry the operator bundle scoped to
# their project (TITAN-BCK-021); their org-role names come from the token.
AGENT_ROLE = "operator"

# Security-sensitive resources the cross-resource read wildcard (*:read) must
# NOT reach (Vol 8 §4.2): the audit log and secret plaintext are carved out of
# general "read everything" scopes — only an explicit grant (or the full "*"
# owner wildcard) reaches them, so viewer/operator/maintainer's *:read cannot
# read the audit log.
SENSITIVE_RESOURCES = frozenset({"audit", "secret"})


def permission_granted(required: str, granted: set[str]) -> bool:
    """True if ``required`` (resource:action) is covered by ``granted``."""
    if ":" not in required:
        return required in granted
    resource, action = required.split(":", 1)
    candidates = {"*", required, f"{resource}:*"}
    if resource not in SENSITIVE_RESOURCES:
        # Cross-resource action wildcard only for non-sensitive resources.
        candidates.add(f"*:{action}")
    return bool(granted & candidates)


def unauthorized(detail: str) -> ProblemException:
    return ProblemException(
        status=401,
        title="Authentication required",
        detail=detail,
        problem_type=problem_type("unauthorized"),
        headers={"WWW-Authenticate": "Bearer"},
    )


def forbidden(permission: str) -> ProblemException:
    return ProblemException(
        status=403,
        title="Forbidden",
        detail=f"Missing required permission: {permission}",
        problem_type=problem_type("forbidden"),
    )


def not_found(resource: str = "resource") -> ProblemException:
    """Object-scope failure — 404, never 403, so existence isn't leaked."""
    return ProblemException(
        status=404,
        title="Not found",
        detail=f"No such {resource}.",
        problem_type=problem_type("not-found"),
    )


class PermissionResolver:
    """Resolves a principal's effective permissions, cached per request.

    USERS: org-wide permissions come from the role names in the JWT; project
    permissions add the user's per-project role grants.

    AGENTS (machine principals, TITAN-BCK-021): agents have NO org-wide
    authority — their operator bundle applies ONLY to their bound project.
    An agent's org_permissions is always empty, and project_permissions is
    the operator bundle for its ``project_id`` alone, so it is 404'd on every
    other project (no cross-project access). Until the agent→project binding
    is populated (Phase 26, when the agents table exists) an agent's
    project_id is None, so it is fail-closed: no project permissions anywhere.
    """

    def __init__(self, engine: AsyncEngine, principal: Principal) -> None:
        self._engine = engine
        self._principal = principal
        self._is_agent = principal.kind == "agent"
        self._org_perms: set[str] | None = None
        self._project_perms: dict[uuid.UUID, set[str]] = {}

    async def _bundle(self, role_names: list[str]) -> set[str]:
        if not role_names:
            return set()
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT permissions FROM roles WHERE name = ANY(:names)"),
                {"names": role_names},
            )
            perms: set[str] = set()
            for (permissions,) in rows:
                perms.update(permissions)
            return perms

    async def org_permissions(self) -> set[str]:
        # Agents are project-scoped machines: no org-wide authority.
        if self._is_agent:
            return set()
        if self._org_perms is None:
            self._org_perms = await self._bundle(self._principal.roles)
        return self._org_perms

    async def project_permissions(self, project_id: uuid.UUID) -> set[str]:
        if project_id not in self._project_perms:
            if self._is_agent:
                # Operator bundle applies only to the agent's bound project.
                if self._principal.project_id == project_id:
                    self._project_perms[project_id] = await self._bundle(self._principal.roles)
                else:
                    self._project_perms[project_id] = set()
            else:
                base = set(await self.org_permissions())
                async with self._engine.connect() as conn:
                    rows = await conn.execute(
                        text(
                            "SELECT r.name FROM user_roles ur JOIN roles r "
                            "ON r.id = ur.role_id "
                            "WHERE ur.user_id = :uid AND ur.project_id = :pid"
                        ),
                        {"uid": self._principal.sub, "pid": project_id},
                    )
                    project_role_names = [name for (name,) in rows]
                base |= await self._bundle(project_role_names)
                self._project_perms[project_id] = base
        return self._project_perms[project_id]

    async def has(self, permission: str, project_id: uuid.UUID | None = None) -> bool:
        granted = (
            await self.project_permissions(project_id)
            if project_id is not None
            else await self.org_permissions()
        )
        return permission_granted(permission, granted)


async def require_object_in_project(
    resolver: "PermissionResolver",
    object_project_id: uuid.UUID | None,
    path_project_id: uuid.UUID,
    resource: str = "resource",
) -> None:
    """Second-half IDOR guard for a specific object id (TITAN-SEC-032).

    Call from a handler after loading an object by id: raises 404 if the
    object is missing OR belongs to a different project than the path scope.
    Existence is never confirmed to an out-of-scope caller.
    """
    if object_project_id is None or object_project_id != path_project_id:
        raise not_found(resource)
    if not await resolver.project_permissions(path_project_id):
        raise not_found(resource)


def get_principal(request: Request) -> Principal:
    """Require an authenticated principal (set by the Phase-8 auth slot)."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise unauthorized(getattr(request.state, "auth_error", None) or "Authentication required.")
    return principal


def get_resolver(request: Request) -> PermissionResolver:
    """One resolver per request (its caches live for the request)."""
    existing: PermissionResolver | None = getattr(request.state, "rbac_resolver", None)
    if existing is not None:
        return existing
    resolver = PermissionResolver(request.app.state.engine, get_principal(request))
    request.state.rbac_resolver = resolver
    return resolver


class Requires:
    """The single authorization enforcement point (§3.4).

    Usage: ``dependencies=[Depends(Requires("task:transition"))]`` or as a
    parameter dependency. Project-scoped routes pass ``project_param`` (a path
    param name) so project-level role grants are consulted.
    """

    #: read at import by the route-guard lint to prove a route is protected.
    __titan_requires__ = True

    def __init__(self, permission: str, project_param: str | None = None) -> None:
        self.permission = permission
        self.project_param = project_param

    async def __call__(self, request: Request) -> Principal:
        principal = get_principal(request)
        resolver = get_resolver(request)

        if self.project_param is None:
            # Org-scoped route: missing permission is a plain 403.
            if not await resolver.has(self.permission):
                raise forbidden(self.permission)
            return principal

        raw = request.path_params.get(self.project_param)
        if raw is None:
            raise not_found("project")
        try:
            project_id = uuid.UUID(str(raw))
        except ValueError as exc:
            raise not_found("project") from exc

        # Object-level authorization (TITAN-SEC-032, no IDOR): if the caller
        # has NO permissions in this project (no org-wide role AND no role on
        # this project), the project is invisible — 404, never 403, so its
        # existence is not leaked. Visible-but-underpowered is a 403.
        granted = await resolver.project_permissions(project_id)
        if not granted:
            raise not_found("project")
        if not permission_granted(self.permission, granted):
            raise forbidden(self.permission)
        return principal


class RequiresAuth:
    """Authenticated but permission-free (self-service routes: /me, /revoke).

    Still a real guard — the route-guard lint accepts it — but grants any
    authenticated principal.
    """

    __titan_requires__ = True

    async def __call__(self, request: Request) -> Principal:
        return get_principal(request)


# Reusable singletons for the common "just authenticated" case.
require_authenticated = RequiresAuth()

CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Resolver = Annotated[PermissionResolver, Depends(get_resolver)]

AuthorizeObject = Callable[[Request], Awaitable[None]]

F = Callable[..., Awaitable[object]]


def mark_public(func: F) -> F:
    """Mark an endpoint as intentionally unauthenticated (pre-auth: token
    minting, health probes). The route-guard lint records these explicitly
    so 'unprotected' is a deliberate, reviewable choice — not an omission."""
    func.__titan_public__ = True  # type: ignore[attr-defined]
    return func
