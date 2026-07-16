"""Admin-risk approval gate + rate-limit tests (Phase 19; Vol 7 §4.4,
TITAN-INT-022, TITAN-SEC-031). Needs the disposable DB (schema marker).

Covers the DoD: an admin-risk call without approval yields PERMISSION_DENIED
plus a pending-approval record, and succeeds after approval; write-risk rate
limits fire; and the human approve/deny endpoints resolve records with RBAC.
"""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.db import uuid7
from titan.gateway.rbac import granting_permissions, permission_granted
from titan.tools import repository as tools_repo
from titan.tools.approvals import _create_pending
from titan.tools.bus import SlidingWindowRateLimiter, ToolBus, ToolCaller
from titan.tools.envelopes import new_call
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolRegistry, ToolSpec
from titan.tools.service import build_bus

from .conftest import run_async

pytestmark = pytest.mark.schema

_SCOPES = frozenset({"admin:run", "test:run"})


def _admin_registry(calls: dict[str, int]) -> ToolRegistry:
    registry = ToolRegistry()

    async def provision(args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
        calls["provisioned"] += 1
        return ExecutionOutcome(result={"region": args["region"]})

    registry.register(
        ToolSpec(
            # deploy.* is in the devops allowlist (Vol 8 §4.3), so the ALLOWLIST
            # passes and the admin-RISK gate is what blocks — the flow under
            # test. A cloud.* name would be denied earlier by the matrix.
            name="deploy.production",
            provider="builtin:deploy",
            description="Trigger a production deploy (admin risk).",
            risk="admin",
            required_scopes=("admin:run",),
            input_schema={
                "type": "object",
                "properties": {"region": {"type": "string"}},
                "required": ["region"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"region": {"type": "string"}},
                "required": ["region"],
            },
            executor=provision,
        )
    )
    return registry


def _caller(project: uuid.UUID, role: str = "devops") -> ToolCaller:
    return ToolCaller(
        agent_id="agent.devops.1",
        role=role,
        project_id=project,
        granted_scopes=_SCOPES,
        task_id=uuid.uuid4(),
    )


def _call(caller: ToolCaller, region: str = "us-east-1") -> Any:
    return new_call(
        "deploy.production",
        {"region": region},
        project_id=caller.project_id,
        agent_id=caller.agent_id,
        task_id=caller.task_id,
    )


def _pending_count(engine: AsyncEngine, project: uuid.UUID, status: str = "pending") -> int:
    async def q() -> int:
        async with engine.connect() as conn:
            r = await conn.execute(
                text("SELECT count(*) FROM tool_approvals WHERE project_id = :p AND status = :s"),
                {"p": project, "s": status},
            )
            return int(r.scalar_one())

    return int(run_async(q))


def test_admin_call_denied_and_creates_single_pending(migrated_engine: AsyncEngine) -> None:
    calls = {"provisioned": 0}
    bus = build_bus(migrated_engine, registry=_admin_registry(calls))
    caller = _caller(uuid.uuid4())

    first = run_async(lambda: bus.invoke(_call(caller), caller))
    assert first.code == "PERMISSION_DENIED"
    assert "awaiting human review" in (first.message or "")
    assert calls["provisioned"] == 0
    assert _pending_count(migrated_engine, caller.project_id) == 1

    # An identical looping call dedupes onto the SAME pending, not a new one.
    second = run_async(lambda: bus.invoke(_call(caller), caller))
    assert second.code == "PERMISSION_DENIED"
    assert _pending_count(migrated_engine, caller.project_id) == 1


def test_admin_call_succeeds_after_approval_and_is_single_use(
    migrated_engine: AsyncEngine,
) -> None:
    calls = {"provisioned": 0}
    bus = build_bus(migrated_engine, registry=_admin_registry(calls))
    caller = _caller(uuid.uuid4())

    denied = run_async(lambda: bus.invoke(_call(caller), caller))
    assert denied.code == "PERMISSION_DENIED"

    # Approve the pending record (human path).
    async def approve() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE tool_approvals SET status = 'approved', resolved_at = now() "
                    "WHERE project_id = :p AND status = 'pending'"
                ),
                {"p": caller.project_id},
            )

    run_async(approve)

    approved = run_async(lambda: bus.invoke(_call(caller), caller))
    assert approved.status == "ok"
    assert approved.result == {"region": "us-east-1"}
    assert calls["provisioned"] == 1
    assert _pending_count(migrated_engine, caller.project_id, "consumed") == 1

    # Single-use: the SAME call again must re-request approval (fresh pending).
    reused = run_async(lambda: bus.invoke(_call(caller), caller))
    assert reused.code == "PERMISSION_DENIED"
    assert calls["provisioned"] == 1
    assert _pending_count(migrated_engine, caller.project_id) == 1


def test_denied_record_keeps_rejecting_without_new_pending(
    migrated_engine: AsyncEngine,
) -> None:
    calls = {"provisioned": 0}
    bus = build_bus(migrated_engine, registry=_admin_registry(calls))
    caller = _caller(uuid.uuid4())

    run_async(lambda: bus.invoke(_call(caller), caller))

    async def deny() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE tool_approvals SET status = 'denied', resolved_at = now() "
                    "WHERE project_id = :p AND status = 'pending'"
                ),
                {"p": caller.project_id},
            )

    run_async(deny)

    after = run_async(lambda: bus.invoke(_call(caller), caller))
    assert after.code == "PERMISSION_DENIED"
    assert "was denied" in (after.message or "")
    assert calls["provisioned"] == 0
    # No fresh pending is spawned while a denial stands.
    assert _pending_count(migrated_engine, caller.project_id) == 0


# --- rate limiting (§4.4 write auto-approved but rate-limited) ----------------


def test_rate_limiter_reclaims_drained_windows() -> None:
    # A re-checked drained key is reclaimed (not left as a stale empty deque),
    # and its window never accretes expired timestamps (review: memory leak).
    clock = {"t": 0.0}
    limiter = SlidingWindowRateLimiter(now=lambda: clock["t"])
    assert run_async(lambda: limiter.allow("a:p", 5)) is True
    assert len(limiter._hits["a:p"]) == 1
    clock["t"] += 61.0  # the window ages out
    assert run_async(lambda: limiter.allow("a:p", 5)) is True
    # The stale timestamp was pruned — exactly one fresh entry, not two.
    assert len(limiter._hits["a:p"]) == 1
    # A merely-inspected (never-allowed) key is never materialized as a phantom.
    assert "never:p" not in limiter._hits


def test_write_tool_rate_limit_fires(migrated_engine: AsyncEngine) -> None:
    clock = {"t": 1000.0}
    limiter = SlidingWindowRateLimiter(now=lambda: clock["t"])
    calls = {"n": 0}
    registry = ToolRegistry()

    async def writer(_args: dict[str, Any], _ctx: ToolExecutionContext) -> ExecutionOutcome:
        calls["n"] += 1
        return ExecutionOutcome(result={})

    registry.register(
        ToolSpec(
            name="git.commit",
            provider="builtin:git",
            description="commit",
            risk="write",
            required_scopes=("test:run",),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            executor=writer,
            rate_limit_per_min=3,
        )
    )
    bus = ToolBus(registry, migrated_engine, rate_limiter=limiter)
    caller = ToolCaller(
        agent_id="agent.backend.1",
        role="backend",
        project_id=uuid.uuid4(),
        granted_scopes=_SCOPES,
        task_id=uuid.uuid4(),
    )

    def commit() -> Any:
        return run_async(
            lambda: bus.invoke(
                new_call(
                    "git.commit",
                    {},
                    project_id=caller.project_id,
                    agent_id=caller.agent_id,
                    task_id=caller.task_id,
                ),
                caller,
            )
        )

    assert [commit().status for _ in range(3)] == ["ok", "ok", "ok"]
    throttled = commit()
    assert throttled.code == "TOOL_UNAVAILABLE" and "rate limit" in (throttled.message or "")
    assert calls["n"] == 3  # the throttled call never dispatched

    clock["t"] += 61.0  # the window rolls forward → budget refreshes
    assert commit().status == "ok"
    assert calls["n"] == 4


# --- the human resolution endpoints (RBAC + lifecycle) ------------------------


@pytest.fixture
def approval_api(api: dict[str, Any]) -> dict[str, Any]:
    """Seed a project + a pending approval row, on top of the shared `api`
    fixture (5 roles + tokens + engine)."""
    engine = api["engine"]
    project_id = uuid7()
    approval_id = uuid7()

    async def seed() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO projects (id, name, display_name, brief, created_by) "
                    "SELECT :id, :name, 'Approvals', 'phase 19', u.id FROM users u "
                    "WHERE u.email LIKE 'owner-%' LIMIT 1"
                ),
                {"id": project_id, "name": f"appr-{project_id}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO tool_approvals (id, project_id, agent_id, tool, args_hash,"
                    " args_redacted, status) VALUES (:id, :p, 'agent.devops.1', 'cloud.provision',"
                    " 'abc', '{\"region\": \"us-east-1\"}'::jsonb, 'pending')"
                ),
                {"id": approval_id, "p": project_id},
            )

    run_async(seed)
    return {**api, "project_id": project_id, "approval_id": approval_id}


def test_list_and_approve_endpoints_rbac(approval_api: dict[str, Any]) -> None:
    client: TestClient = approval_api["client"]
    project_id = approval_api["project_id"]
    approval_id = approval_api["approval_id"]
    token = approval_api["token_for"]

    base = f"/api/v1/projects/{project_id}/tool-approvals"
    # Viewer may read (list) but not resolve.
    listed = client.get(base, headers=token("viewer"))
    assert listed.status_code == 200
    assert any(item["id"] == str(approval_id) for item in listed.json()["items"])

    denied = client.post(f"{base}/{approval_id}:approve", json={}, headers=token("viewer"))
    assert denied.status_code == 403

    # Maintainer may approve (Vol 8 §4.2 "A" workflow).
    ok = client.post(f"{base}/{approval_id}:approve", json={}, headers=token("maintainer"))
    assert ok.status_code == 200
    body = ok.json()
    assert body["status"] == "approved" and body["resolved_by"] is not None

    # Re-approving an already-resolved record is a 409.
    again = client.post(f"{base}/{approval_id}:approve", json={}, headers=token("maintainer"))
    assert again.status_code == 409


def test_deny_endpoint_and_missing_is_404(approval_api: dict[str, Any]) -> None:
    client: TestClient = approval_api["client"]
    project_id = approval_api["project_id"]
    approval_id = approval_api["approval_id"]
    token = approval_api["token_for"]
    base = f"/api/v1/projects/{project_id}/tool-approvals"

    denied = client.post(f"{base}/{approval_id}:deny", json={}, headers=token("admin"))
    assert denied.status_code == 200 and denied.json()["status"] == "denied"

    # A non-existent approval id → 404 (no existence leak).
    missing = client.post(f"{base}/{uuid.uuid4()}:deny", json={}, headers=token("admin"))
    assert missing.status_code == 404


# --- §7.1 approval notifications (Vol 4 §7.1: Toast + bell + Attention Queue) --


def _seed_project(engine: AsyncEngine) -> uuid.UUID:
    """A real projects row: user_roles.project_id is an FK, so a per-project
    grant needs one to exist."""
    project_id = uuid7()

    async def seed() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO projects (id, name, display_name, brief, created_by) "
                    "SELECT :id, :name, 'Notify', 'phase 19', u.id FROM users u "
                    "WHERE u.email LIKE 'owner-%' LIMIT 1"
                ),
                {"id": project_id, "name": f"notify-{project_id}"},
            )

    run_async(seed)
    return project_id


def _approval_notifications(engine: AsyncEngine, project_id: uuid.UUID) -> list[Any]:
    """The notifications raised for THIS project's approvals. Keyed off the deep
    link because notifications are user-scoped and carry no project column."""

    async def q() -> list[Any]:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT user_id, category, severity, title, body, link FROM notifications "
                    "WHERE link LIKE :prefix"
                ),
                {"prefix": f"/projects/{project_id}/approvals%"},
            )
            return list(result)

    return list(run_async(q))


def _named_recipients(user_ids: dict[str, uuid.UUID], notified: set[uuid.UUID]) -> set[str]:
    """This fixture's users that were notified. The assertions are scoped to them
    on purpose: `migrated_engine` is session-scoped, so users seeded by EARLIER
    tests keep their org-wide grants and are legitimately notified for every
    project. Asserting a global count would make these tests order-dependent."""
    return {name for name, uid in user_ids.items() if uid in notified}


def test_new_pending_notifies_only_humans_holding_tool_approval_write(
    api: dict[str, Any],
) -> None:
    engine: AsyncEngine = api["engine"]
    user_ids: dict[str, uuid.UUID] = api["user_ids"]
    project_id = _seed_project(engine)
    bus = build_bus(engine, registry=_admin_registry({"provisioned": 0}))
    caller = _caller(project_id)

    result = run_async(lambda: bus.invoke(_call(caller), caller))
    assert result.code == "PERMISSION_DENIED"

    notified = {row.user_id for row in _approval_notifications(engine, project_id)}
    # Owner (`*`), Admin and Maintainer hold tool_approval:write (Vol 8 §4.2).
    # Operator is the AGENT bundle and Viewer is read-only — neither is notified,
    # which is also what keeps agents off the recipient list.
    assert _named_recipients(user_ids, notified) == {"owner", "admin", "maintainer"}


def test_approval_notification_content_and_deep_link(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    user_ids: dict[str, uuid.UUID] = api["user_ids"]
    project_id = _seed_project(engine)
    bus = build_bus(engine, registry=_admin_registry({"provisioned": 0}))
    caller = _caller(project_id)

    run_async(lambda: bus.invoke(_call(caller), caller))

    async def approval_id() -> uuid.UUID:
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT id FROM tool_approvals WHERE project_id = :p"), {"p": project_id}
            )
            return uuid.UUID(str(row.scalar_one()))

    pending_id = run_async(approval_id)
    rows = _approval_notifications(engine, project_id)
    row = next(r for r in rows if r.user_id == user_ids["admin"])

    assert row.category == "approval" and row.severity == "critical"
    assert "deploy.production" in row.title and "agent.devops.1" in row.title
    # Enough context to decide without leaving the bell: tool, agent, task, args.
    assert "deploy.production" in row.body and "agent.devops.1" in row.body
    assert str(caller.task_id) in row.body and "us-east-1" in row.body
    assert row.link == f"/projects/{project_id}/approvals?approval={pending_id}"


def test_dedupe_hit_creates_no_second_notification(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    project_id = _seed_project(engine)
    bus = build_bus(engine, registry=_admin_registry({"provisioned": 0}))
    caller = _caller(project_id)

    run_async(lambda: bus.invoke(_call(caller), caller))
    first = len(_approval_notifications(engine, project_id))
    assert first > 0

    # A looping agent re-issuing the IDENTICAL call dedupes onto the same pending
    # record and must NOT re-raise the bell.
    run_async(lambda: bus.invoke(_call(caller), caller))
    assert len(_approval_notifications(engine, project_id)) == first
    assert _pending_count(engine, project_id) == 1


def test_project_scoped_grant_is_notified_only_in_its_own_project(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    user_ids: dict[str, uuid.UUID] = api["user_ids"]
    granted_project = _seed_project(engine)
    other_project = _seed_project(engine)
    # `scoped` holds no org-wide role — only maintainer on `granted_project`.
    api["grant_project_role"]("scoped", str(granted_project), "maintainer")
    bus = build_bus(engine, registry=_admin_registry({"provisioned": 0}))

    granted_caller = _caller(granted_project)
    run_async(lambda: bus.invoke(_call(granted_caller), granted_caller))
    other_caller = _caller(other_project)
    run_async(lambda: bus.invoke(_call(other_caller), other_caller))

    in_granted = {r.user_id for r in _approval_notifications(engine, granted_project)}
    in_other = {r.user_id for r in _approval_notifications(engine, other_project)}
    assert user_ids["scoped"] in in_granted  # per-project grant counts
    assert user_ids["scoped"] not in in_other  # and does not leak to other projects
    # Org-wide holders are still notified in both.
    assert user_ids["admin"] in in_granted and user_ids["admin"] in in_other


def test_zero_approvers_still_writes_the_pending_record(migrated_engine: AsyncEngine) -> None:
    """A project no human can approve is not an error: the pending row is still
    written (it stays visible in the approvals list) and no bell is raised.

    Runs inside a rolled-back transaction because it must remove every grant to
    make the recipient set genuinely empty — `migrated_engine` is session-scoped,
    so a committed DELETE would corrupt other tests.
    """
    project_id = uuid7()
    caller = _caller(project_id)
    call = _call(caller)

    async def check() -> None:
        async with migrated_engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.execute(text("DELETE FROM user_roles"))
                assert await tools_repo.approvers_for_project(conn, project_id) == []

                created = await _create_pending(conn, call, "hash-zero", {"region": "us-east-1"})
                assert created is not None  # the record exists despite no recipients

                count = await conn.execute(
                    text("SELECT count(*) FROM notifications WHERE link LIKE :prefix"),
                    {"prefix": f"/projects/{project_id}/approvals%"},
                )
                assert int(count.scalar_one()) == 0
            finally:
                await trans.rollback()

    run_async(check)


def test_granting_permissions_drives_the_reverse_lookup(migrated_engine: AsyncEngine) -> None:
    """The recipient query matches roles by `granting_permissions`, so this pins
    the wildcard forms it must accept and its equivalence with the forward check."""
    assert granting_permissions("tool_approval:write") == {
        "*",
        "tool_approval:write",
        "tool_approval:*",
        "*:write",
    }
    # Sensitive resources carve out the cross-resource wildcard (Vol 8 §4.2).
    assert "*:read" not in granting_permissions("audit:read")

    candidates = granting_permissions("tool_approval:write")
    for granted in ({"*"}, {"tool_approval:write"}, {"tool_approval:*"}, {"*:write"}, {"*:read"}):
        assert permission_granted("tool_approval:write", granted) == bool(granted & candidates)
