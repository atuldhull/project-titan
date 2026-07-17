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
from typing import Annotated, Any

import structlog
from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.gateway.problems import ProblemException, problem_type
from titan.identity.service import Principal
from titan.observability.context import bind_log_project

logger = structlog.get_logger("titan.gateway.rbac")

# Machine principals (agent service JWTs) carry the operator bundle scoped to
# their project (TITAN-BCK-021); their org-role names come from the token.
AGENT_ROLE = "operator"

# Security-sensitive resources the cross-resource read wildcard (*:read) must
# NOT reach (Vol 8 §4.2): the audit log and secret plaintext are carved out of
# general "read everything" scopes — only an explicit grant (or the full "*"
# owner wildcard) reaches them, so viewer/operator/maintainer's *:read cannot
# read the audit log.
#
# `logs` joins them in Phase 62. System logs are an org-wide operator surface
# with no project tenancy (Vol 4 §6.7): they carry request/trace ids, internal
# error text and every service's stack traces, and redaction does NOT run on
# the logging path (Vol 9 §6.3 claims it does — `titan.tools.redaction` guards
# audit rows and tool envelopes only). Without this entry `logs:read` would be
# granted by the `*:read` that viewer AND operator — the bundle every agent
# runs as — already hold, so minting the permission would itself have published
# the firehose. Adding the resource here is the only thing that carves it out;
# the name you pick cannot.
SENSITIVE_RESOURCES = frozenset({"audit", "logs", "secret"})

# The canonical set of RBAC scopes the host recognises. Permissions are otherwise
# free-form strings, so this is the reference list a PLUGIN MANIFEST is validated
# against: a manifest requesting a scope that is not here fails installation with an
# actionable "unknown scope" error naming the field (Vol 7 §3.2, TITAN-INT-010).
KNOWN_SCOPES: frozenset[str] = frozenset(
    {
        "project:read",
        "project:write",
        "project:create",
        "project:delete",
        "task:read",
        "task:create",
        "task:update",
        "task:transition",
        "task:assign",
        "task:cancel",
        "run:create",
        "run:cancel",
        "run:read",
        "message:read",
        "message:write",
        "decision:write",
        "agent:configure",
        "audit:read",
        "tool_approval:write",
        "memory:read",
        "memory:write",
        "memory:admin",
        "kb:propose",
        "kb:approve",
        "pipeline:read",
        "pipeline:advance",
        "pipeline:override",
        "workspace:read",
        "workspace:write",
        "terminal:run",
        "docker:run",
        "browser:use",
        "web:search",
        "web:fetch",
        "deploy:local",
        "plugin:read",
        "plugin:admin",
    }
)


def granting_permissions(required: str) -> frozenset[str]:
    """Every permission string that would grant ``required`` — the wildcard forms
    included.

    Extracted from ``permission_granted`` so a REVERSE lookup ("which roles hold
    this permission?", e.g. the approval-notification recipient query in
    ``titan.tools.repository``) is driven by the same rules as the forward check
    and cannot drift from it when the wildcard semantics change.
    """
    if ":" not in required:
        return frozenset({required})
    resource, action = required.split(":", 1)
    candidates = {"*", required, f"{resource}:*"}
    if resource not in SENSITIVE_RESOURCES:
        # Cross-resource action wildcard only for non-sensitive resources.
        candidates.add(f"*:{action}")
    return frozenset(candidates)


def permission_granted(required: str, granted: set[str]) -> bool:
    """True if ``required`` (resource:action) is covered by ``granted``."""
    return bool(granted & granting_permissions(required))


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

    _UNRESOLVED = object()  # sentinel: agent→project binding not yet looked up

    def __init__(self, engine: AsyncEngine, principal: Principal) -> None:
        self._engine = engine
        self._principal = principal
        self._is_agent = principal.kind == "agent"
        self._org_perms: set[str] | None = None
        self._project_perms: dict[uuid.UUID, set[str]] = {}
        self._agent_project: Any = self._UNRESOLVED

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

    async def _bound_project(self) -> uuid.UUID | None:
        """The agent's bound project: the principal's explicit binding when
        present, else the agents row (Phase 26) looked up ONCE per request.
        Fail-closed: no row / no binding → None → no project permissions."""
        if self._principal.project_id is not None:
            return self._principal.project_id
        if self._agent_project is self._UNRESOLVED:
            self._agent_project = None
            if self._principal.agent_id is not None:
                async with self._engine.connect() as conn:
                    row = await conn.execute(
                        text("SELECT project_id FROM agents WHERE id = :aid"),
                        {"aid": self._principal.agent_id},
                    )
                    found = row.one_or_none()
                self._agent_project = found[0] if found is not None else None
        return self._agent_project  # type: ignore[no-any-return]

    async def bound_project(self) -> uuid.UUID | None:
        """The agent's bound project (the only scope its operator bundle applies
        to, TITAN-BCK-021); None for a user principal or an unbound agent. Lets a
        handler authorize an agent for an org-wide-but-governed action (e.g. a KB
        proposal) by its project-derived authority."""
        if self._is_agent:
            return await self._bound_project()
        return None

    async def project_permissions(self, project_id: uuid.UUID) -> set[str]:
        if project_id not in self._project_perms:
            if self._is_agent:
                # Operator bundle applies only to the agent's bound project.
                if await self._bound_project() == project_id:
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


async def authorize_in_project(
    request: Request,
    project_id: uuid.UUID,
    permission: str,
    *,
    resource: str = "resource",
) -> None:
    """Object-level authorization for a route whose project is only known after
    the target row is loaded (object-addressed routes like /tasks/{id} or
    /runs/{id}, whose project comes from the row, not the path).

    Resolves the caller's permissions IN THAT PROJECT: no visibility there is a
    404 (existence never leaked, TITAN-SEC-032); visible-but-underpowered is a
    403. Works for org-wide and project-scoped principals alike.
    """
    resolver = get_resolver(request)
    granted = await resolver.project_permissions(project_id)
    if not granted:
        raise not_found(resource)
    if not permission_granted(permission, granted):
        raise forbidden(permission)
    bind_log_project(project_id)


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
        bind_log_project(project_id)
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
