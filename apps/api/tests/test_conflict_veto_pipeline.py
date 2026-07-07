"""The Security veto blocks the merge at the pipeline level (TITAN-AGT-018) — needs
the disposable test DB + the API fixture.

Covers the DoD: a vetoed artifact cannot be merged (the ``approve`` → ``done``
transition) by ANY path — not the reviewer, not even a CEO acting as approver —
and only a human override lifts the block so the merge can proceed."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import run_async
from titan.conflict import service as conflict

pytestmark = pytest.mark.schema

_MB = {"provider": "ollama", "model": "q"}


def _project(api: dict[str, Any]) -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    r = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _task(api: dict[str, Any], project_id: str) -> str:
    r = api["client"].post(
        f"/api/v1/projects/{project_id}/tasks",
        json={"title": "ship feature"},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _agent(api: dict[str, Any], role: str) -> str:
    r = api["client"].post(
        "/api/v1/agents",
        json={"role": role, "name": f"{role}-bot", "model_binding": _MB},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _transition(api: dict[str, Any], task_id: str, action: str, *, actor: str | None = None) -> Any:
    body: dict[str, Any] = {"action": action}
    if actor is not None:
        body["actor_agent_id"] = actor
    return api["client"].post(
        f"/api/v1/tasks/{task_id}:transition", json=body, headers=api["token_for"]("owner")
    )


def _drive_to_in_review(api: dict[str, Any], task_id: str, assignee: str) -> None:
    assert _transition(api, task_id, "ready").status_code == 200
    assert _transition(api, task_id, "assign", actor=assignee).status_code == 200
    assert _transition(api, task_id, "start").status_code == 200
    assert _transition(api, task_id, "submit_review").status_code == 200


def test_security_veto_blocks_merge_by_any_path(api: dict[str, Any], migrated_engine: Any) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    assignee = _agent(api, "backend")
    reviewer = _agent(api, "reviewer")
    ceo = _agent(api, "ceo")
    _drive_to_in_review(api, tid, assignee)

    artifact = f"task:{tid}"

    async def raise_veto() -> None:
        async with migrated_engine.begin() as conn:
            await conflict.raise_veto(
                conn,
                uuid.UUID(pid),
                artifact_ref=artifact,
                raised_by="security",
                reason="hardcoded secret in diff",
                task_id=uuid.UUID(tid),
            )

    run_async(raise_veto)

    # The reviewer's approve (the merge into `done`) is blocked by the veto.
    blocked = _transition(api, tid, "approve", actor=reviewer)
    assert blocked.status_code == 409, blocked.text
    assert "veto" in blocked.text.lower()

    # Even a CEO acting as the approver (a "CEO-approved release job") is blocked —
    # the gate consults only the veto table, never who is approving (§7.4).
    ceo_blocked = _transition(api, tid, "approve", actor=ceo)
    assert ceo_blocked.status_code == 409, ceo_blocked.text

    # The task never moved — it is still awaiting review, not done.
    board = api["client"].get(f"/api/v1/tasks/{tid}", headers=api["token_for"]("owner"))
    assert board.json()["status"] == "in_review"

    # The human override (relayed by the CEO) lifts the veto…
    async def override() -> None:
        async with migrated_engine.begin() as conn:
            await conflict.override_veto(
                conn, uuid.UUID(pid), artifact_ref=artifact, risk_memo="client accepts the risk"
            )

    run_async(override)

    # …and now the merge proceeds.
    merged = _transition(api, tid, "approve", actor=reviewer)
    assert merged.status_code == 200, merged.text
    assert merged.json()["status"] == "done"


def test_unvetoed_merge_is_unaffected(api: dict[str, Any]) -> None:
    """The gate is a no-op when no veto is active — a normal review still merges."""
    pid = _project(api)
    tid = _task(api, pid)
    assignee = _agent(api, "backend")
    reviewer = _agent(api, "reviewer")
    _drive_to_in_review(api, tid, assignee)
    assert _transition(api, tid, "approve", actor=reviewer).json()["status"] == "done"
