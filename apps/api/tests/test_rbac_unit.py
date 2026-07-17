"""RBAC matcher + route-guard unit tests (no DB, no network)."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI

from tests.conftest import run_async
from titan.gateway.problems import ProblemException
from titan.gateway.rbac import (
    Requires,
    RequiresAuth,
    get_principal,
    mark_public,
    permission_granted,
    require_object_in_project,
)
from titan.gateway.route_guard import unprotected_routes
from titan.identity.service import Principal
from titan.main import create_app
from titan.settings import Settings


class FakeResolver:
    """Stand-in for PermissionResolver — no DB."""

    def __init__(self, org: set[str], project: dict[uuid.UUID, set[str]] | None = None) -> None:
        self._org = org
        self._project = project or {}

    async def has(self, permission: str, project_id: uuid.UUID | None = None) -> bool:
        granted = self._project.get(project_id, set()) if project_id else self._org
        return permission_granted(permission, granted)

    async def project_permissions(self, project_id: uuid.UUID) -> set[str]:
        return self._project.get(project_id, set())


def _request(principal: Principal | None, resolver: Any, **path_params: str) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(principal=principal, rbac_resolver=resolver, auth_error=None),
        path_params=path_params,
    )


PRINCIPAL = Principal(sub=uuid.uuid4(), kind="user", roles=["viewer"], scope="user", jti="j")

# The §9.3 seed bundles (source of truth: scripts/seed.py ROLE_PERMISSIONS).
SEED_BUNDLES = {
    "owner": {"*"},
    "admin": {
        "project:*",
        "agent:configure",
        "task:*",
        "run:cancel",
        "run:create",
        "message:write",
        "decision:write",
        "audit:read",
        "logs:read",
        "*:read",
    },
    "maintainer": {
        "project:write",
        "task:create",
        "task:update",
        "task:transition",
        "task:assign",
        "task:cancel",
        "decision:write",
        "message:write",
        "*:read",
    },
    "operator": {"task:transition", "run:create", "message:write", "*:read"},
    "viewer": {"*:read"},
}


@pytest.mark.parametrize(
    ("required", "role", "expected"),
    [
        ("task:transition", "owner", True),  # * grants everything
        ("task:transition", "operator", True),  # explicit
        ("task:transition", "viewer", False),  # viewer is read-only
        ("project:read", "viewer", True),  # *:read
        ("project:write", "viewer", False),
        ("project:write", "maintainer", True),
        ("project:delete", "maintainer", False),  # not in maintainer bundle
        ("project:delete", "admin", True),  # project:* covers it
        ("task:create", "operator", False),  # operator can't create tasks
        ("run:create", "operator", True),
        ("agent:configure", "admin", True),
        ("agent:configure", "maintainer", False),
        ("secret:read", "owner", True),  # owner wildcard (policy denies elsewhere)
    ],
)
def test_permission_matcher_against_seed_bundles(required: str, role: str, expected: bool) -> None:
    assert permission_granted(required, SEED_BUNDLES[role]) is expected


def test_wildcard_resource_and_action() -> None:
    assert permission_granted("task:read", {"task:*"}) is True
    assert permission_granted("task:read", {"*:read"}) is True
    assert permission_granted("task:read", {"task:read"}) is True
    assert permission_granted("task:read", {"run:*", "*:write"}) is False


def test_sensitive_resources_excluded_from_wildcard_read() -> None:
    # Vol 8 §4.2: *:read must NOT grant audit/secret read…
    assert permission_granted("audit:read", {"*:read"}) is False
    assert permission_granted("secret:read", {"*:read"}) is False
    # …but explicit grants and the full owner wildcard still reach them.
    assert permission_granted("audit:read", {"audit:read"}) is True
    assert permission_granted("audit:read", {"audit:*"}) is True
    assert permission_granted("audit:read", {"*"}) is True  # owner
    assert permission_granted("secret:read", {"*"}) is True  # owner (no endpoint exists)


def test_system_logs_are_not_reachable_by_the_read_wildcard() -> None:
    """Phase 62. The whole safety of GET /api/v1/logs rests on this one line.

    `logs` is in SENSITIVE_RESOURCES, so `*:read` — which viewer, operator,
    maintainer and admin all carry — must not reach the system log history.
    Delete that entry and every one of those bundles silently gains the
    firehose while this file's other tests stay green.
    """
    assert permission_granted("logs:read", {"*:read"}) is False
    # `operator` is the bundle every AGENT runs as (AGENT_ROLE), and it holds
    # *:read. Agents must not be able to read the system logs back.
    assert permission_granted("logs:read", {"task:transition", "run:create", "*:read"}) is False
    # Owner's full wildcard and an explicit grant still reach it.
    assert permission_granted("logs:read", {"*"}) is True
    assert permission_granted("logs:read", {"logs:read"}) is True
    assert permission_granted("logs:read", {"logs:*"}) is True


def test_route_guard_passes_on_real_app() -> None:
    app = create_app(Settings(otel_exporter_otlp_endpoint="", _env_file=None))
    assert unprotected_routes(app) == []


def test_route_guard_catches_an_unprotected_route() -> None:
    """The route-guard lint must FAIL on a deliberately unguarded route."""
    app = create_app(Settings(otel_exporter_otlp_endpoint="", _env_file=None))

    @app.get("/api/v1/oops-no-guard")
    async def oops() -> dict[str, str]:
        return {"leak": "everything"}

    offenders = unprotected_routes(app)
    assert any("/api/v1/oops-no-guard" in o for o in offenders)


def test_route_guard_recurses_into_included_routers() -> None:
    """Regression: FastAPI wraps include_router children in _IncludedRouter;
    the walker MUST descend it or the whole check is a silent no-op."""
    from fastapi import APIRouter

    app = create_app(Settings(otel_exporter_otlp_endpoint="", _env_file=None))
    # The real app's routes are all nested; a naive top-level scan sees none.
    assert unprotected_routes(app) == []  # real routes ARE inspected, all clean

    unguarded = APIRouter()

    @unguarded.get("/api/v1/pwned")
    async def pwned() -> dict[str, str]:
        return {"secret": "PWNED"}

    app.include_router(unguarded)
    assert any("/api/v1/pwned" in o for o in unprotected_routes(app))


def test_route_guard_flags_unguarded_websocket() -> None:
    app = create_app(Settings(otel_exporter_otlp_endpoint="", _env_file=None))

    @app.websocket("/api/v1/leak-ws")
    async def leak(ws: object) -> None:  # pragma: no cover - never invoked
        pass

    assert any("leak-ws" in o for o in unprotected_routes(app))


def test_route_guard_accepts_requires_and_public_and_auth() -> None:
    # Match TITAN's docs/openapi urls so the framework Starlette routes land
    # in PUBLIC_PATHS (the real app is configured this way).
    app = FastAPI(openapi_url="/api/openapi.json", docs_url="/api/docs", redoc_url=None)

    @app.get("/guarded", dependencies=[Depends(Requires("task:read"))])
    async def guarded() -> dict[str, str]:
        return {}

    @app.get("/authed", dependencies=[Depends(RequiresAuth())])
    async def authed() -> dict[str, str]:
        return {}

    @app.get("/explicitly-public")
    @mark_public
    async def pub() -> dict[str, str]:
        return {}

    assert unprotected_routes(app) == []


# --- Requires / ownership dependency logic (fake resolver, no DB) ----------


def test_get_principal_raises_401_when_absent() -> None:
    with pytest.raises(ProblemException) as exc:
        get_principal(_request(None, None))
    assert exc.value.status == 401


def test_requires_org_scope_allows_and_denies() -> None:
    async def scenario() -> None:
        allow = _request(PRINCIPAL, FakeResolver({"task:read", "*:read"}))
        assert await Requires("task:read")(allow) is PRINCIPAL

        deny = _request(PRINCIPAL, FakeResolver({"*:read"}))
        with pytest.raises(ProblemException) as exc:
            await Requires("task:write")(deny)
        assert exc.value.status == 403

    run_async(scenario)


def test_requires_project_scope_404_when_invisible_403_when_underpowered() -> None:
    async def scenario() -> None:
        pid = uuid.uuid4()
        # No perms in this project at all → 404 (invisible, no IDOR leak).
        invisible = _request(PRINCIPAL, FakeResolver(org=set(), project={}), pid=str(pid))
        with pytest.raises(ProblemException) as exc404:
            await Requires("item:read", project_param="pid")(invisible)
        assert exc404.value.status == 404

        # Visible but lacks the specific action → 403.
        underpowered = _request(
            PRINCIPAL, FakeResolver(org=set(), project={pid: {"*:read"}}), pid=str(pid)
        )
        with pytest.raises(ProblemException) as exc403:
            await Requires("item:write", project_param="pid")(underpowered)
        assert exc403.value.status == 403

        # Visible and permitted → allowed.
        ok = _request(
            PRINCIPAL, FakeResolver(org=set(), project={pid: {"item:read"}}), pid=str(pid)
        )
        assert await Requires("item:read", project_param="pid")(ok) is PRINCIPAL

    run_async(scenario)


def test_requires_project_scope_404_on_bad_uuid_or_missing_param() -> None:
    async def scenario() -> None:
        no_param = _request(PRINCIPAL, FakeResolver(set()))
        with pytest.raises(ProblemException) as missing:
            await Requires("item:read", project_param="pid")(no_param)
        assert missing.value.status == 404

        bad = _request(PRINCIPAL, FakeResolver(set()), pid="not-a-uuid")
        with pytest.raises(ProblemException) as exc:
            await Requires("item:read", project_param="pid")(bad)
        assert exc.value.status == 404

    run_async(scenario)


def test_requires_auth_returns_principal() -> None:
    async def scenario() -> None:
        assert await RequiresAuth()(_request(PRINCIPAL, FakeResolver(set()))) is PRINCIPAL

    run_async(scenario)


def test_require_object_in_project_404_cases() -> None:
    async def scenario() -> None:
        pid = uuid.uuid4()
        resolver: Any = FakeResolver(org=set(), project={pid: {"item:read"}})
        with pytest.raises(ProblemException) as missing:
            await require_object_in_project(resolver, None, pid, "item")
        assert missing.value.status == 404
        with pytest.raises(ProblemException) as mismatch:
            await require_object_in_project(resolver, uuid.uuid4(), pid, "item")
        assert mismatch.value.status == 404
        await require_object_in_project(resolver, pid, pid, "item")  # in-scope: no raise

    run_async(scenario)
