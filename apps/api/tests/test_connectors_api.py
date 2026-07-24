"""Connector integration through REAL core surfaces (Phase 54; TITAN-INT-040/042).

Real DB, real plugin subprocess. Two DoD proofs that require core, with ZERO core changes:

* GitHub webhook through the actual ``/hooks/github`` route: a valid X-Hub-Signature-256 is
  verified by core, delivered to the enabled connector subprocess, and translated into a
  ``vcs.*`` event on the bus; a bad signature is rejected 401 (and logged by core).
* The Slack-button approval path against the REAL approval machinery: an admin-risk tool
  blocks on a pending approval; the connector's interactivity handler resolves it (attributed
  to the approver); the next call consumes the approval and dispatches.

Skips cleanly if a connector directory is absent (INT-011)."""

import hashlib
import hmac
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from tests.connector_loader import connector_dir, load_connector

pytestmark = pytest.mark.schema

_WEBHOOK_SECRET = "gh-webhook-key"


# --- GitHub webhook through the real core route (TITAN-INT-040) --------------


@pytest.fixture
def github_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    src = connector_dir("github")
    if not (src / "titan-plugin.yaml").exists():
        pytest.skip("github connector not installed")
    plugins_dir = tmp_path / "plugins"
    shutil.copytree(src, plugins_dir / "github")
    monkeypatch.setenv("TITAN_PLUGINS_ENABLED", "true")
    monkeypatch.setenv("TITAN_PLUGINS_DIR", str(plugins_dir))
    monkeypatch.setenv("TITAN_PLUGIN_SECRET_GITHUB_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    return plugins_dir


def _enable_github(api: dict[str, Any]) -> None:
    admin = api["token_for"]("admin")
    assert api["client"].post("/api/v1/plugins:discover", headers=admin).status_code == 200
    assert (
        api["client"].post("/api/v1/plugins/titan.github/install", headers=admin).status_code == 200
    )
    resp = api["client"].post("/api/v1/plugins/titan.github/enable", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "enabled"


def test_github_webhook_verified_and_translated_to_a_vcs_event(
    github_env: Path, api: dict[str, Any]
) -> None:
    _enable_github(api)
    body = json.dumps(
        {
            "ref": "refs/heads/main",
            "before": "a",
            "after": "b",
            "commits": [{}, {}],
            "pusher": {"name": "dev"},
            "repository": {"full_name": "o/r"},
        }
    ).encode("utf-8")
    signature = "sha256=" + hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()

    ok = api["client"].post(
        "/api/v1/plugins/titan.github/hooks/hooks/github",
        content=body,
        headers={"x-hub-signature-256": signature, "content-type": "application/json"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["result"]["published"] == ["vcs.push"]

    # The connector published vcs.push onto the events domain stream via capabilities.publish.
    async def count_events() -> int:
        async with api["engine"].connect() as conn:
            return int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM outbox "
                            "WHERE payload->'data'->>'type' = 'vcs.push'"
                        )
                    )
                ).scalar_one()
            )

    assert run_async(count_events) >= 1


def test_github_webhook_with_a_bad_signature_is_rejected_401(
    github_env: Path, api: dict[str, Any]
) -> None:
    _enable_github(api)
    body = json.dumps(
        {"ref": "refs/heads/main", "commits": [], "repository": {"full_name": "o/r"}}
    ).encode("utf-8")
    bad = api["client"].post(
        "/api/v1/plugins/titan.github/hooks/hooks/github",
        content=body,
        headers={"x-hub-signature-256": "sha256=deadbeef", "content-type": "application/json"},
    )
    assert bad.status_code == 401  # core verifies + rejects before the plugin ever runs


def _push_body() -> bytes:
    return json.dumps(
        {"ref": "refs/heads/main", "commits": [], "repository": {"full_name": "o/r"}}
    ).encode("utf-8")


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_github_webhook_unsigned_is_rejected_401(github_env: Path, api: dict[str, Any]) -> None:
    _enable_github(api)
    # No signature header at all (SEC-075: unsigned → 401).
    resp = api["client"].post(
        "/api/v1/plugins/titan.github/hooks/hooks/github",
        content=_push_body(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401


def test_github_webhook_replay_is_rejected(github_env: Path, api: dict[str, Any]) -> None:
    _enable_github(api)
    body = _push_body()
    headers = {"x-hub-signature-256": _sign(body), "content-type": "application/json"}
    url = "/api/v1/plugins/titan.github/hooks/hooks/github"
    first = api["client"].post(url, content=body, headers=headers)
    assert first.status_code == 200
    # An identical, correctly-signed delivery is a replay (SEC-075).
    replay = api["client"].post(url, content=body, headers=headers)
    assert replay.status_code == 401


def test_github_webhook_replay_with_rotated_delivery_id_still_rejected(
    github_env: Path, api: dict[str, Any]
) -> None:
    # Phase-70 review CRITICAL regression: dedup keys on the SIGNATURE, so
    # rotating the unsigned X-GitHub-Delivery header does NOT defeat it.
    _enable_github(api)
    body = _push_body()
    sig = _sign(body)
    url = "/api/v1/plugins/titan.github/hooks/hooks/github"
    first = api["client"].post(
        url,
        content=body,
        headers={"x-hub-signature-256": sig, "x-github-delivery": str(uuid.uuid4())},
    )
    assert first.status_code == 200
    # Same signed body, FRESH delivery-id header — the old (broken) code keyed on
    # the delivery id and would let this through; it must now be 401.
    replay = api["client"].post(
        url,
        content=body,
        headers={"x-hub-signature-256": sig, "x-github-delivery": str(uuid.uuid4())},
    )
    assert replay.status_code == 401


def test_github_webhook_stale_timestamp_is_rejected(github_env: Path, api: dict[str, Any]) -> None:
    import time

    _enable_github(api)
    body = _push_body()
    url = "/api/v1/plugins/titan.github/hooks/hooks/github"
    stale = api["client"].post(
        url,
        content=body,
        headers={
            "x-hub-signature-256": _sign(body),
            "x-titan-timestamp": str(int(time.time()) - 10_000),  # > 5 min skew
            "content-type": "application/json",
        },
    )
    assert stale.status_code == 401
    # A fresh timestamp on the same (correctly-signed) delivery is accepted.
    fresh = api["client"].post(
        url,
        content=body,
        headers={
            "x-hub-signature-256": _sign(body),
            "x-titan-timestamp": str(int(time.time())),
            "content-type": "application/json",
        },
    )
    assert fresh.status_code == 200


# --- Slack approval → real approval machinery (TITAN-INT-042) ----------------


def test_slack_button_resolves_a_real_admin_gate_and_the_blocked_call_proceeds(
    migrated_engine: AsyncEngine,
) -> None:
    from urllib.parse import urlencode

    from titan.tools.approvals import DbApprovalGate, resolve_approval
    from titan.tools.bus import ToolBus, ToolCaller
    from titan.tools.envelopes import new_call
    from titan.tools.service import ExecutionOutcome, ToolExecutionContext, ToolRegistry, ToolSpec

    chat_plugin: Any = load_connector("chat", "chat_plugin")
    chat_http: Any = load_connector("chat", "chat_http")

    project_id, task_id = uuid.uuid4(), uuid.uuid4()
    dispatched = {"n": 0}

    async def _exec(_a: dict[str, Any], _c: ToolExecutionContext) -> ExecutionOutcome:
        dispatched["n"] += 1
        return ExecutionOutcome(result={"ok": True})

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="github.merge_pr",
            provider="plugin:titan.github",
            description="",
            risk="admin",  # the admin gate the Slack button will resolve
            required_scopes=(),
            input_schema={"type": "object", "additionalProperties": True},
            output_schema={"type": "object", "additionalProperties": True},
            executor=_exec,
        )
    )
    bus = ToolBus(registry, migrated_engine, approval_gate=DbApprovalGate(migrated_engine))
    caller = ToolCaller(
        agent_id="agent.devops.1",
        role="devops",
        project_id=project_id,
        granted_scopes=frozenset(),
        task_id=task_id,
        initiator_chain=("user",),
    )

    def _call() -> Any:
        return run_async(
            lambda: bus.invoke(
                new_call(
                    "github.merge_pr",
                    {"owner": "o", "repo": "r", "number": 1},
                    project_id=project_id,
                    agent_id="agent.devops.1",
                    task_id=task_id,
                ),
                caller,
            )
        )

    # 1) First call blocks: an admin gate pending approval is created.
    blocked = _call()
    assert blocked.code == "PERMISSION_DENIED" and dispatched["n"] == 0

    async def _pending_id() -> uuid.UUID:
        async with migrated_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT id FROM tool_approvals WHERE project_id = :p AND status = 'pending'"
                    ),
                    {"p": project_id},
                )
            ).one()
        return uuid.UUID(str(row.id))

    approval_id = run_async(_pending_id)
    approver = _seed_user(migrated_engine)

    # 2) The connector's interactivity handler resolves it via the REAL core resolver,
    #    attributed to the TITAN user the Slack approver maps to.
    def resolver(pid: str, aid: str, action: str, who: str) -> dict[str, Any]:
        run_async(
            lambda: _resolve(
                migrated_engine,
                resolve_approval,
                uuid.UUID(aid),
                uuid.UUID(pid),
                action,
                uuid.UUID(who),
            )
        )
        return {"status": 200}

    connector = chat_plugin.ChatConnector(
        config={
            "SLACK_USER_MAP": json.dumps({"U9": str(approver)}),
            "SLACK_DEFAULT_APPROVER": str(approver),
        },
        http=chat_http.FakeTransport(),
        resolve=resolver,
        sleep=lambda _s: None,
    )
    payload = {"user": {"id": "U9"}, "actions": [{"value": f"approve|{project_id}|{approval_id}"}]}
    handled = connector.on_webhook(
        "/hooks/slack/interactivity", {"raw": urlencode({"payload": json.dumps(payload)})}
    )
    assert handled["handled"] and handled["action"] == "approved"

    # 3) The next identical call consumes the approval and DISPATCHES.
    proceeded = _call()
    assert proceeded.status == "ok" and dispatched["n"] == 1

    # 4) The approval is attributed to the approver in the record.
    async def _resolved_by() -> str:
        async with migrated_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT resolved_by FROM tool_approvals WHERE id = :id"),
                    {"id": approval_id},
                )
            ).one()
        return str(row.resolved_by)

    assert run_async(_resolved_by) == str(approver)


async def _resolve(
    engine: AsyncEngine,
    resolve_fn: Any,
    aid: uuid.UUID,
    pid: uuid.UUID,
    action: str,
    who: uuid.UUID,
) -> None:
    async with engine.begin() as conn:
        await resolve_fn(conn, approval_id=aid, project_id=pid, action=action, resolved_by=who)


def _seed_user(engine: AsyncEngine) -> uuid.UUID:
    from titan.db import uuid7

    uid = uuid7()

    async def insert() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :e, 'Approver', 'local', :s)"
                ),
                {"id": uid, "e": f"approver-{uid}@titan.local", "s": str(uid)},
            )

    run_async(insert)
    return uid
