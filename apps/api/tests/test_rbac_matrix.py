"""Permission-matrix + IDOR tests (TITAN-SEC-030/032): every route × role.

Seeds one user per §9.3 role, mints their access token via the app keyring,
and drives permission-gated fixture routes (each protected by Requires(...))
— asserting allow (2xx) / deny (403) per the seed bundle, and that
out-of-scope project resources return 404, never 403 (no IDOR). Real routes
are all pre-auth or RequiresAuth at this phase; the first Requires("r:a")
domain routes arrive in Phase 11 and join this matrix then."""

import uuid
from collections.abc import Iterator
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import Depends, Request
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import run_async
from titan import clients
from titan.db import uuid7
from titan.gateway.rbac import Requires, get_resolver, require_object_in_project
from titan.main import create_app
from titan.settings import Settings

pytestmark = pytest.mark.schema

ROLES = ["owner", "admin", "maintainer", "operator", "viewer"]

# Fixture org-scoped routes: permission -> which roles the seed bundle allows.
ORG_ROUTES = {
    "task:transition": {"owner", "admin", "maintainer", "operator"},
    "task:create": {"owner", "admin", "maintainer"},
    "project:write": {"owner", "admin", "maintainer"},
    "project:delete": {"owner", "admin"},
    "agent:configure": {"owner", "admin"},
    "run:create": {"owner", "admin", "operator"},  # maintainer bundle lacks it
    # Vol 8 §4.2: audit read is Owner + Admin ONLY — *:read must NOT reach it.
    "audit:read": {"owner", "admin"},
    # Vol 7 §4.4: admin-risk tool approvals resolved by Owner/Admin/Maintainer.
    "tool_approval:write": {"owner", "admin", "maintainer"},
}


def _fixture_routes(app: Any) -> None:
    for perm in ORG_ROUTES:
        # bind perm in a default arg so each closure keeps its own value
        async def handler(_r: Request, _p: str = perm) -> dict[str, str]:
            return {"ok": _p}

        app.add_api_route(
            f"/api/v1/probe/{perm.replace(':', '-')}",
            handler,
            methods=["GET"],
            dependencies=[Depends(Requires(perm))],
        )

    # Project-scoped route for IDOR: item:read within a project.
    async def get_item(request: Request, pid: str, item_id: str) -> dict[str, str]:
        resolver = get_resolver(request)
        # object-level: the item's real project comes from the DB fixture.
        item_project = request.app.state.item_project.get(item_id)
        await require_object_in_project(
            resolver,
            uuid.UUID(item_project) if item_project else None,
            uuid.UUID(pid),
            "item",
        )
        return {"item": item_id}

    app.add_api_route(
        "/api/v1/projects/{pid}/items/{item_id}",
        get_item,
        methods=["GET"],
        dependencies=[Depends(Requires("item:read", project_param="pid"))],
    )


@pytest.fixture
def ctx(
    migrated_engine: Any, schema_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(clients, "create_redis", lambda _settings: fake)

    # Seed the 5 roles + one user per role (org-wide grant) + two projects.
    user_ids: dict[str, uuid.UUID] = {}
    project_a, project_b = uuid7(), uuid7()

    async def seed() -> None:
        from scripts.seed import ROLE_PERMISSIONS

        async with migrated_engine.begin() as conn:
            for name, perms in ROLE_PERMISSIONS.items():
                await conn.execute(
                    text(
                        "INSERT INTO roles (id, name, permissions) VALUES (:id, :n, :p) "
                        "ON CONFLICT (name) DO UPDATE SET permissions = :p"
                    ),
                    {"id": uuid7(), "n": name, "p": __import__("json").dumps(perms)},
                )
            owner_uid = None
            for role in ROLES:
                uid = uuid7()
                user_ids[role] = uid
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, display_name, auth_provider, auth_subject)"
                        " VALUES (:id, :email, :dn, 'local', :subj)"
                    ),
                    {"id": uid, "email": f"{role}-{uid}@titan.local", "dn": role, "subj": str(uid)},
                )
                await conn.execute(
                    text(
                        "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                        "SELECT :rid, :uid, r.id, NULL FROM roles r WHERE r.name = :role"
                    ),
                    {"rid": uuid7(), "uid": uid, "role": role},
                )
                if role == "owner":
                    owner_uid = uid
            # Two projects owned by the owner.
            for pid, name in ((project_a, "proj-a"), (project_b, "proj-b")):
                await conn.execute(
                    text(
                        "INSERT INTO projects (id, name, display_name, brief, created_by) "
                        "VALUES (:id, :n, :n, 'x', :uid)"
                    ),
                    {"id": pid, "n": f"{name}-{pid}", "uid": owner_uid},
                )
            # A user with ONLY a project-A maintainer role (no org role).
            scoped_uid = uuid7()
            user_ids["scoped_a"] = scoped_uid
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :email, 'scoped', 'local', :subj)"
                ),
                {
                    "id": scoped_uid,
                    "email": f"scoped-{scoped_uid}@titan.local",
                    "subj": str(scoped_uid),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                    "SELECT :rid, :uid, r.id, :pid FROM roles r WHERE r.name = 'maintainer'"
                ),
                {"rid": uuid7(), "uid": scoped_uid, "pid": project_a},
            )

    run_async(seed)

    settings = Settings(database_url=schema_url, otel_exporter_otlp_endpoint="", _env_file=None)
    app = create_app(settings)
    _fixture_routes(app)
    # item X lives in project A (used by the IDOR test).
    app.state.item_project = {"item-x": str(project_a)}

    with TestClient(app, raise_server_exceptions=False) as client:

        def token_for(role: str, roles: list[str] | None = None) -> dict[str, str]:
            access, _ = app.state.keyring.mint(
                sub=str(user_ids[role]),
                roles=roles if roles is not None else [role] if role in ROLES else [],
                scope="user",
            )
            return {"Authorization": f"Bearer {access}"}

        yield {
            "client": client,
            "token_for": token_for,
            "project_a": str(project_a),
            "project_b": str(project_b),
        }


@pytest.mark.parametrize("role", ROLES)
def test_org_permission_matrix(ctx: dict[str, Any], role: str) -> None:
    client, token_for = ctx["client"], ctx["token_for"]
    headers = token_for(role)
    for perm, allowed_roles in ORG_ROUTES.items():
        path = f"/api/v1/probe/{perm.replace(':', '-')}"
        status = client.get(path, headers=headers).status_code
        if role in allowed_roles:
            assert status == 200, f"{role} should be allowed {perm}, got {status}"
        else:
            assert status == 403, f"{role} should be denied {perm}, got {status}"


def test_unauthenticated_gets_401_not_403(ctx: dict[str, Any]) -> None:
    resp = ctx["client"].get("/api/v1/probe/task-transition")
    assert resp.status_code == 401
    assert resp.json()["type"] == "https://titan.dev/errors/unauthorized"


def test_project_scoped_role_grants_only_its_project(ctx: dict[str, Any]) -> None:
    client = ctx["client"]
    headers = client_scoped = ctx["token_for"]("scoped_a", roles=[])  # no org role
    # maintainer on project A → item:read on A works…
    ok = client.get(f"/api/v1/projects/{ctx['project_a']}/items/item-x", headers=headers)
    assert ok.status_code == 200
    del client_scoped


def test_idor_out_of_scope_project_returns_404(ctx: dict[str, Any]) -> None:
    """The project-A-only user requesting a project-B resource must get 404,
    never 403 (TITAN-SEC-032) — project B is invisible to them."""
    client = ctx["client"]
    headers = ctx["token_for"]("scoped_a", roles=[])
    resp = client.get(f"/api/v1/projects/{ctx['project_b']}/items/item-x", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["type"] == "https://titan.dev/errors/not-found"


def test_idor_wrong_project_for_existing_item_returns_404(ctx: dict[str, Any]) -> None:
    """Even an owner (org-wide) asking for item-x under project B — where it
    does NOT live — gets 404: the object-scope check catches the mismatch."""
    client = ctx["client"]
    headers = ctx["token_for"]("owner")
    resp = client.get(f"/api/v1/projects/{ctx['project_b']}/items/item-x", headers=headers)
    assert resp.status_code == 404


def test_org_wide_owner_sees_item_in_its_real_project(ctx: dict[str, Any]) -> None:
    client = ctx["client"]
    headers = ctx["token_for"]("owner")
    resp = client.get(f"/api/v1/projects/{ctx['project_a']}/items/item-x", headers=headers)
    assert resp.status_code == 200


def test_agent_gets_no_cross_project_access(ctx: dict[str, Any]) -> None:
    """TITAN-BCK-021: an agent's operator bundle is project-scoped, never
    org-wide. With no project binding yet (Phase 26) an agent is fail-closed —
    404 on every project route, no cross-project operator access."""
    app = ctx["client"].app
    access, _ = app.state.keyring.mint(
        sub=str(uuid7()), roles=["operator"], scope="agent", agent_id=str(uuid7())
    )
    headers = {"Authorization": f"Bearer {access}"}
    # Agent org-scoped route: operator bundle must NOT apply org-wide → 403.
    org = ctx["client"].get("/api/v1/probe/task-transition", headers=headers)
    assert org.status_code == 403
    # Agent project route (unbound) → 404 (project invisible, no cross-project).
    proj = ctx["client"].get(f"/api/v1/projects/{ctx['project_a']}/items/item-x", headers=headers)
    assert proj.status_code == 404
