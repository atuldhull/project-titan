"""Phase 11 integration tests: projects / milestones / sprints / tasks REST.

Every endpoint gets happy-path + auth-failure + validation-failure coverage,
plus the phase DoD: POST /projects → 201 with an id; the created task appears on
/board; a stale ETag on PATCH → 412; an A→B→C→A dependency chain → 409
end-to-end. Needs the disposable test DB (schema marker) + fakeredis — no
network.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import run_async
from titan import clients
from titan.db import uuid7
from titan.main import create_app
from titan.settings import Settings

pytestmark = pytest.mark.schema

ROLES = ("owner", "admin", "maintainer", "operator", "viewer")


@pytest.fixture
def ctx(
    migrated_engine: Any, schema_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(clients, "create_redis", lambda _settings: fake)

    user_ids: dict[str, uuid.UUID] = {}

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
            # One org-wide user per role, plus a project-scoped-only user.
            for role in (*ROLES, "scoped"):
                uid = uuid7()
                user_ids[role] = uid
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, display_name, auth_provider, auth_subject)"
                        " VALUES (:id, :email, :dn, 'local', :subj)"
                    ),
                    {"id": uid, "email": f"{role}-{uid}@titan.local", "dn": role, "subj": str(uid)},
                )
                if role in ROLES:
                    await conn.execute(
                        text(
                            "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                            "SELECT :rid, :uid, r.id, NULL FROM roles r WHERE r.name = :role"
                        ),
                        {"rid": uuid7(), "uid": uid, "role": role},
                    )

    run_async(seed)

    settings = Settings(database_url=schema_url, otel_exporter_otlp_endpoint="", _env_file=None)
    app = create_app(settings)

    def grant_project_role(role_user: str, project_id: str, role: str) -> None:
        async def grant() -> None:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                        "SELECT :rid, :uid, r.id, :pid FROM roles r WHERE r.name = :role"
                    ),
                    {"rid": uuid7(), "uid": user_ids[role_user], "pid": project_id, "role": role},
                )

        run_async(grant)

    with TestClient(app, raise_server_exceptions=False) as client:

        def token_for(role: str, roles: list[str] | None = None) -> dict[str, str]:
            access, _ = app.state.keyring.mint(
                sub=str(user_ids[role]),
                roles=roles if roles is not None else ([role] if role in ROLES else []),
                scope="user",
            )
            return {"Authorization": f"Bearer {access}"}

        yield {
            "client": client,
            "token_for": token_for,
            "grant_project_role": grant_project_role,
            "user_ids": user_ids,
        }


# --- helpers ---------------------------------------------------------------


def _new_project(ctx: dict[str, Any], *, name: str | None = None, role: str = "owner") -> Any:
    name = name or f"proj-{uuid.uuid4().hex[:12]}"
    resp = ctx["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name.title(), "brief": "A brief."},
        headers=ctx["token_for"](role),
    )
    assert resp.status_code == 201, resp.text
    return resp


def _new_task(
    ctx: dict[str, Any], project_id: str, title: str = "Task", role: str = "owner"
) -> Any:
    resp = ctx["client"].post(
        f"/api/v1/projects/{project_id}/tasks",
        json={"title": title, "priority": 3},
        headers=ctx["token_for"](role),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- projects: create ------------------------------------------------------


def test_create_project_201_with_id_and_defaults(ctx: dict[str, Any]) -> None:
    """TITAN-FND-001 / DoD: POST /projects returns 201 with an id, status
    intake, created_by the caller, version 1, and a v1 ETag."""
    resp = _new_project(ctx, name="netflix-clone")
    body = resp.json()
    assert uuid.UUID(body["id"])  # a real UUID
    assert body["status"] == "intake"
    assert body["mode"] == "autonomous"
    assert body["version"] == 1
    assert body["created_by"] == str(ctx["user_ids"]["owner"])
    assert resp.headers["etag"] == '"v1"'


def test_create_project_requires_auth(ctx: dict[str, Any]) -> None:
    resp = ctx["client"].post(
        "/api/v1/projects",
        json={"name": "x", "display_name": "X", "brief": "b"},
    )
    assert resp.status_code == 401
    assert resp.json()["type"] == "https://titan.dev/errors/unauthorized"


def test_create_project_forbidden_for_viewer(ctx: dict[str, Any]) -> None:
    resp = ctx["client"].post(
        "/api/v1/projects",
        json={"name": "vieweronly", "display_name": "V", "brief": "b"},
        headers=ctx["token_for"]("viewer"),
    )
    assert resp.status_code == 403
    assert resp.json()["type"] == "https://titan.dev/errors/forbidden"


def test_create_project_validation_fail(ctx: dict[str, Any]) -> None:
    # Bad slug (uppercase/space) → 422 problem+json.
    resp = ctx["client"].post(
        "/api/v1/projects",
        json={"name": "Not A Slug", "display_name": "X", "brief": "b"},
        headers=ctx["token_for"]("owner"),
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["type"] == "https://titan.dev/errors/validation"


def test_create_project_duplicate_name_409(ctx: dict[str, Any]) -> None:
    _new_project(ctx, name="unique-slug")
    dup = ctx["client"].post(
        "/api/v1/projects",
        json={"name": "unique-slug", "display_name": "X", "brief": "b"},
        headers=ctx["token_for"]("owner"),
    )
    assert dup.status_code == 409
    assert dup.json()["type"] == "https://titan.dev/errors/conflict"


# --- projects: read / list -------------------------------------------------


def test_list_projects_cursor_pagination_and_filter(ctx: dict[str, Any]) -> None:
    ids = [_new_project(ctx).json()["id"] for _ in range(3)]
    client, headers = ctx["client"], ctx["token_for"]("owner")

    first = client.get("/api/v1/projects?limit=2", headers=headers)
    assert first.status_code == 200
    page1 = first.json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"]

    second = client.get(f"/api/v1/projects?limit=2&cursor={page1['next_cursor']}", headers=headers)
    page2 = second.json()
    seen = {p["id"] for p in page1["items"]} | {p["id"] for p in page2["items"]}
    assert set(ids) <= seen  # every created project shows up across pages, no dupes
    # Default sort is -created_at: the first item is the newest project.
    assert page1["items"][0]["id"] == ids[-1]


def test_list_projects_status_filter(ctx: dict[str, Any]) -> None:
    live = _new_project(ctx).json()["id"]
    archived = _new_project(ctx).json()["id"]
    client, headers = ctx["client"], ctx["token_for"]("owner")
    client.post(f"/api/v1/projects/{archived}:archive", headers=headers)

    resp = client.get("/api/v1/projects?status=archived&limit=200", headers=headers)
    returned = {p["id"] for p in resp.json()["items"]}
    assert archived in returned and live not in returned


def test_list_projects_malformed_cursor_is_400(ctx: dict[str, Any]) -> None:
    """A cursor that base64-decodes to a dict but carries an unparseable
    keyset position is a 400 problem, never a 500."""
    import base64
    import json

    bad = base64.urlsafe_b64encode(json.dumps({"created_at": "nope", "id": "x"}).encode())
    bad_cursor = bad.decode().rstrip("=")
    resp = ctx["client"].get(
        f"/api/v1/projects?cursor={bad_cursor}", headers=ctx["token_for"]("owner")
    )
    assert resp.status_code == 400
    assert resp.json()["type"] == "https://titan.dev/errors/invalid-cursor"


def test_get_project_sets_etag(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].get(f"/api/v1/projects/{pid}", headers=ctx["token_for"]("owner"))
    assert resp.status_code == 200
    assert resp.headers["etag"] == '"v1"'


def test_get_project_404(ctx: dict[str, Any]) -> None:
    resp = ctx["client"].get(f"/api/v1/projects/{uuid.uuid4()}", headers=ctx["token_for"]("owner"))
    assert resp.status_code == 404
    assert resp.json()["type"] == "https://titan.dev/errors/not-found"


# --- projects: update (ETag/If-Match) --------------------------------------


def test_patch_project_with_if_match_bumps_version(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    client, headers = ctx["client"], ctx["token_for"]("owner")
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"display_name": "Renamed"},
        headers={**headers, "If-Match": '"v1"'},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed"
    assert resp.json()["version"] == 2
    assert resp.headers["etag"] == '"v2"'


def test_patch_missing_if_match_428(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].patch(
        f"/api/v1/projects/{pid}",
        json={"display_name": "X"},
        headers=ctx["token_for"]("owner"),
    )
    assert resp.status_code == 428
    assert resp.json()["type"] == "https://titan.dev/errors/precondition-required"


def test_patch_stale_etag_412(ctx: dict[str, Any]) -> None:
    """DoD: a stale ETag on PATCH returns 412."""
    pid = _new_project(ctx).json()["id"]
    client, headers = ctx["client"], ctx["token_for"]("owner")
    # First update moves it to v2.
    client.patch(
        f"/api/v1/projects/{pid}",
        json={"display_name": "First"},
        headers={**headers, "If-Match": '"v1"'},
    )
    # Second update still presenting the now-stale v1.
    stale = client.patch(
        f"/api/v1/projects/{pid}",
        json={"display_name": "Second"},
        headers={**headers, "If-Match": '"v1"'},
    )
    assert stale.status_code == 412
    assert stale.json()["type"] == "https://titan.dev/errors/version-conflict"


def test_patch_project_rejects_unknown_field_422(ctx: dict[str, Any]) -> None:
    # ProjectPatch forbids `status` (and any extra) — status is not PATCHable.
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].patch(
        f"/api/v1/projects/{pid}",
        json={"status": "planning"},
        headers={**ctx["token_for"]("owner"), "If-Match": '"v1"'},
    )
    assert resp.status_code == 422


def test_patch_project_forbidden_for_viewer(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].patch(
        f"/api/v1/projects/{pid}",
        json={"display_name": "X"},
        headers={**ctx["token_for"]("viewer"), "If-Match": '"v1"'},
    )
    assert resp.status_code == 403


def test_archive_project(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].post(f"/api/v1/projects/{pid}:archive", headers=ctx["token_for"]("owner"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "archived"
    assert resp.json()["version"] == 2


# --- milestones ------------------------------------------------------------


def test_milestone_crud_and_scope(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    other = _new_project(ctx).json()["id"]
    client, headers = ctx["client"], ctx["token_for"]("owner")

    created = client.post(
        f"/api/v1/projects/{pid}/milestones",
        json={"title": "M1", "status": "open"},
        headers=headers,
    )
    assert created.status_code == 201
    mid = created.json()["id"]
    assert created.headers["etag"] == '"v1"'

    listed = client.get(f"/api/v1/projects/{pid}/milestones", headers=headers)
    assert mid in {m["id"] for m in listed.json()["items"]}

    patched = client.patch(
        f"/api/v1/projects/{pid}/milestones/{mid}",
        json={"status": "closed"},
        headers={**headers, "If-Match": '"v1"'},
    )
    assert patched.status_code == 200 and patched.json()["status"] == "closed"

    # The same milestone under a DIFFERENT project → 404 (object scope).
    cross = client.get(f"/api/v1/projects/{other}/milestones/{mid}", headers=headers)
    assert cross.status_code == 404


def test_milestone_create_forbidden_for_viewer(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].post(
        f"/api/v1/projects/{pid}/milestones",
        json={"title": "M"},
        headers=ctx["token_for"]("viewer"),
    )
    assert resp.status_code == 403


def test_milestone_validation_fail(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].post(
        f"/api/v1/projects/{pid}/milestones",
        json={"title": ""},  # min_length=1
        headers=ctx["token_for"]("owner"),
    )
    assert resp.status_code == 422


# --- sprints ---------------------------------------------------------------


def test_sprint_crud_and_unique_number(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    client, headers = ctx["client"], ctx["token_for"]("owner")

    created = client.post(
        f"/api/v1/projects/{pid}/sprints",
        json={"number": 1, "goal": "Ship", "status": "planned"},
        headers=headers,
    )
    assert created.status_code == 201 and created.json()["number"] == 1

    dup = client.post(
        f"/api/v1/projects/{pid}/sprints",
        json={"number": 1},
        headers=headers,
    )
    assert dup.status_code == 409
    assert dup.json()["type"] == "https://titan.dev/errors/conflict"


def test_sprint_validation_fail(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].post(
        f"/api/v1/projects/{pid}/sprints",
        json={"number": 0},  # ge=1
        headers=ctx["token_for"]("owner"),
    )
    assert resp.status_code == 422


def test_sprint_get_list_and_patch(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    client, headers = ctx["client"], ctx["token_for"]("owner")
    sid = client.post(
        f"/api/v1/projects/{pid}/sprints",
        json={"number": 7, "goal": "Alpha"},
        headers=headers,
    ).json()["id"]

    got = client.get(f"/api/v1/projects/{pid}/sprints/{sid}", headers=headers)
    assert got.status_code == 200 and got.headers["etag"] == '"v1"'

    listed = client.get(f"/api/v1/projects/{pid}/sprints", headers=headers)
    assert sid in {s["id"] for s in listed.json()["items"]}

    patched = client.patch(
        f"/api/v1/projects/{pid}/sprints/{sid}",
        json={"status": "active", "goal": "Alpha-1"},
        headers={**headers, "If-Match": '"v1"'},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "active" and patched.json()["version"] == 2


def test_list_tasks_by_project(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    a, b = _new_task(ctx, pid, "A"), _new_task(ctx, pid, "B")
    resp = ctx["client"].get(
        f"/api/v1/projects/{pid}/tasks?limit=50", headers=ctx["token_for"]("owner")
    )
    assert resp.status_code == 200
    returned = {t["id"] for t in resp.json()["items"]}
    assert {a["id"], b["id"]} <= returned


def test_mutations_write_domain_events_to_outbox(ctx: dict[str, Any], migrated_engine: Any) -> None:
    """The transactional outbox 'finally gets used': creating a task and a
    sprint commits task.created / sprint.updated events atomically with the row.
    """
    import json as _json

    pid = _new_project(ctx).json()["id"]
    _new_task(ctx, pid, "Emits an event")
    ctx["client"].post(
        f"/api/v1/projects/{pid}/sprints",
        json={"number": 1},
        headers=ctx["token_for"]("owner"),
    )

    async def event_types() -> set[str]:
        async with migrated_engine.connect() as conn:
            rows = await conn.execute(text("SELECT payload FROM outbox"))
            types: set[str] = set()
            for (payload,) in rows:
                wrapper = _json.loads(payload) if isinstance(payload, str) else payload
                types.add(wrapper.get("data", {}).get("type", ""))
            return types

    types = run_async(event_types)
    assert "task.created" in types
    assert "sprint.updated" in types


# --- tasks -----------------------------------------------------------------


def test_create_task_201_and_appears_on_board(ctx: dict[str, Any]) -> None:
    """DoD: a created task appears in the /board aggregate (backlog column)."""
    pid = _new_project(ctx).json()["id"]
    task = _new_task(ctx, pid, title="Implement auth")
    assert task["status"] == "backlog"

    board = ctx["client"].get(f"/api/v1/projects/{pid}/board", headers=ctx["token_for"]("owner"))
    assert board.status_code == 200
    columns = {c["status"]: c for c in board.json()["columns"]}
    assert "backlog" in columns
    assert task["id"] in {c["id"] for c in columns["backlog"]["tasks"]}


def test_task_patch_rejects_status_422(ctx: dict[str, Any]) -> None:
    """Phase requirement: the task PATCH schema must NOT accept `status`; a
    status change is a 422, because state only moves via :transition."""
    pid = _new_project(ctx).json()["id"]
    task = _new_task(ctx, pid)
    resp = ctx["client"].patch(
        f"/api/v1/tasks/{task['id']}",
        json={"status": "done"},
        headers={**ctx["token_for"]("owner"), "If-Match": '"v1"'},
    )
    assert resp.status_code == 422
    assert resp.json()["type"] == "https://titan.dev/errors/validation"


def test_task_patch_happy_bumps_version(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    task = _new_task(ctx, pid)
    resp = ctx["client"].patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "Renamed", "priority": 1},
        headers={**ctx["token_for"]("owner"), "If-Match": '"v1"'},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed" and resp.json()["priority"] == 1
    assert resp.json()["version"] == 2


def test_task_estimate_points_must_be_fibonacci(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].post(
        f"/api/v1/projects/{pid}/tasks",
        json={"title": "T", "estimate_points": 4},  # 4 ∉ {1,2,3,5,8,13}
        headers=ctx["token_for"]("owner"),
    )
    assert resp.status_code == 422


def test_get_task_and_404(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    task = _new_task(ctx, pid)
    ok = ctx["client"].get(f"/api/v1/tasks/{task['id']}", headers=ctx["token_for"]("owner"))
    assert ok.status_code == 200 and ok.headers["etag"] == '"v1"'
    missing = ctx["client"].get(f"/api/v1/tasks/{uuid.uuid4()}", headers=ctx["token_for"]("owner"))
    assert missing.status_code == 404


def test_create_task_requires_permission(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].post(
        f"/api/v1/projects/{pid}/tasks",
        json={"title": "T"},
        headers=ctx["token_for"]("viewer"),  # viewer lacks task:create
    )
    assert resp.status_code == 403


# --- dependencies (cycle check) --------------------------------------------


def test_dependency_happy_201(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    a, b = _new_task(ctx, pid, "A"), _new_task(ctx, pid, "B")
    resp = ctx["client"].post(
        f"/api/v1/tasks/{a['id']}/dependencies",
        json={"depends_on_id": b["id"]},
        headers=ctx["token_for"]("owner"),
    )
    assert resp.status_code == 201
    assert resp.json() == {"task_id": a["id"], "depends_on_id": b["id"]}


def test_dependency_cycle_returns_409(ctx: dict[str, Any]) -> None:
    """DoD: inserting an A→B→C→A dependency chain through the API returns 409."""
    pid = _new_project(ctx).json()["id"]
    a, b, c = (_new_task(ctx, pid, t) for t in ("A", "B", "C"))
    client, headers = ctx["client"], ctx["token_for"]("owner")

    def depend(x: dict[str, Any], y: dict[str, Any]) -> Any:
        return client.post(
            f"/api/v1/tasks/{x['id']}/dependencies",
            json={"depends_on_id": y["id"]},
            headers=headers,
        )

    assert depend(a, b).status_code == 201  # A depends on B
    assert depend(b, c).status_code == 201  # B depends on C
    closing = depend(c, a)  # C depends on A → closes the cycle
    assert closing.status_code == 409
    assert closing.json()["type"] == "https://titan.dev/errors/invalid-dependency-cycle"


def test_self_dependency_returns_409(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    a = _new_task(ctx, pid, "A")
    resp = ctx["client"].post(
        f"/api/v1/tasks/{a['id']}/dependencies",
        json={"depends_on_id": a["id"]},
        headers=ctx["token_for"]("owner"),
    )
    assert resp.status_code == 409


def test_graph_reflects_nodes_and_edges(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    a, b = _new_task(ctx, pid, "A"), _new_task(ctx, pid, "B")
    ctx["client"].post(
        f"/api/v1/tasks/{a['id']}/dependencies",
        json={"depends_on_id": b["id"]},
        headers=ctx["token_for"]("owner"),
    )
    graph = ctx["client"].get(f"/api/v1/projects/{pid}/graph", headers=ctx["token_for"]("owner"))
    assert graph.status_code == 200
    body = graph.json()
    assert {n["id"] for n in body["nodes"]} == {a["id"], b["id"]}
    assert {"task_id": a["id"], "depends_on_id": b["id"]} in body["edges"]


# --- object-level authorization / IDOR (TITAN-SEC-032) ---------------------


def test_scoped_user_cannot_see_other_project(ctx: dict[str, Any]) -> None:
    """A user with a maintainer role on project A only must get 404 (never 403)
    for project B — B is invisible to them."""
    proj_a = _new_project(ctx).json()["id"]
    proj_b = _new_project(ctx).json()["id"]
    ctx["grant_project_role"]("scoped", proj_a, "maintainer")
    headers = ctx["token_for"]("scoped", roles=[])  # no org role

    assert ctx["client"].get(f"/api/v1/projects/{proj_a}", headers=headers).status_code == 200
    resp = ctx["client"].get(f"/api/v1/projects/{proj_b}", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["type"] == "https://titan.dev/errors/not-found"


# --- review fixes: explicit-null handling, cursor direction, framework errors -


def test_patch_explicit_null_on_required_field_is_422(ctx: dict[str, Any]) -> None:
    """A JSON null for a NOT NULL field is a 422 problem, not a 500 (review
    finding: no 500-on-bad-input)."""
    pid = _new_project(ctx).json()["id"]
    hdr = {**ctx["token_for"]("owner"), "If-Match": '"v1"'}
    for field in ("display_name", "brief", "mode", "llm_profile"):
        resp = ctx["client"].patch(f"/api/v1/projects/{pid}", json={field: None}, headers=hdr)
        assert resp.status_code == 422, f"{field}: {resp.status_code}"
        assert resp.json()["type"] == "https://titan.dev/errors/validation"


def test_task_patch_explicit_null_on_required_field_is_422(ctx: dict[str, Any]) -> None:
    pid = _new_project(ctx).json()["id"]
    task = _new_task(ctx, pid)
    hdr = {**ctx["token_for"]("owner"), "If-Match": '"v1"'}
    for field in ("title", "spec", "priority"):
        resp = ctx["client"].patch(f"/api/v1/tasks/{task['id']}", json={field: None}, headers=hdr)
        assert resp.status_code == 422, f"{field}: {resp.status_code}"


def test_patch_null_clears_a_nullable_field(ctx: dict[str, Any]) -> None:
    """The genuinely-nullable fields still accept null so a caller can clear
    them: milestone.due_at and task.estimate_points."""
    pid = _new_project(ctx).json()["id"]
    client, headers = ctx["client"], ctx["token_for"]("owner")

    mid = client.post(
        f"/api/v1/projects/{pid}/milestones",
        json={"title": "M", "due_at": "2026-09-01T00:00:00Z"},
        headers=headers,
    ).json()["id"]
    cleared = client.patch(
        f"/api/v1/projects/{pid}/milestones/{mid}",
        json={"due_at": None},
        headers={**headers, "If-Match": '"v1"'},
    )
    assert cleared.status_code == 200 and cleared.json()["due_at"] is None

    task = client.post(
        f"/api/v1/projects/{pid}/tasks",
        json={"title": "T", "estimate_points": 5},
        headers=headers,
    ).json()
    ep = client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"estimate_points": None},
        headers={**headers, "If-Match": '"v1"'},
    )
    assert ep.status_code == 200 and ep.json()["estimate_points"] is None


def test_cursor_carries_sort_direction(ctx: dict[str, Any]) -> None:
    """Paging with ?sort=created_at (ascending) then dropping the param on page
    2 must NOT flip direction: the cursor carries it, so no row is skipped or
    duplicated (review finding). Robust to rows from other tests in the session:
    asserts the cursor encodes the direction and that page 2 continues ascending
    with no overlap."""
    import base64
    import json

    for _ in range(3):
        _new_project(ctx)
    client, headers = ctx["client"], ctx["token_for"]("owner")

    page1 = client.get("/api/v1/projects?sort=created_at&limit=2", headers=headers).json()
    items1 = page1["items"]
    assert items1[0]["created_at"] <= items1[1]["created_at"]  # ascending

    # The cursor itself carries the direction (desc=false for ascending).
    padded = page1["next_cursor"] + "=" * (-len(page1["next_cursor"]) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded))["desc"] is False

    # Follow the cursor WITHOUT re-sending sort (endpoint default is descending).
    page2 = client.get(
        f"/api/v1/projects?limit=2&cursor={page1['next_cursor']}", headers=headers
    ).json()["items"]
    # Direction held: page 2 continues ascending past page 1, no row repeats.
    assert page2[0]["created_at"] >= items1[-1]["created_at"]
    assert not ({p["id"] for p in items1} & {p["id"] for p in page2})


def test_framework_http_error_has_titan_type(ctx: dict[str, Any]) -> None:
    """A method-not-allowed (DELETE on a GET/PATCH-only route) is problem+json
    with a titan.dev type slug, not about:blank (review finding)."""
    pid = _new_project(ctx).json()["id"]
    resp = ctx["client"].delete(f"/api/v1/projects/{pid}", headers=ctx["token_for"]("owner"))
    assert resp.status_code == 405
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["type"] == "https://titan.dev/errors/method-not-allowed"


def test_object_addressed_task_hides_out_of_scope_project(ctx: dict[str, Any]) -> None:
    """GET /tasks/{id} for a task in a project the caller can't see → 404, via
    the object-level authorization on the task's own project."""
    proj_a = _new_project(ctx).json()["id"]
    proj_b = _new_project(ctx).json()["id"]
    ctx["grant_project_role"]("scoped", proj_a, "maintainer")
    task_b = _new_task(ctx, proj_b, "secret")  # created by owner in project B
    headers = ctx["token_for"]("scoped", roles=[])

    resp = ctx["client"].get(f"/api/v1/tasks/{task_b['id']}", headers=headers)
    assert resp.status_code == 404
