"""Phase 12 integration tests: project messages, the decision log, and per-user
notifications (incl. the TITAN-FRT-038 restart-persistence DoD).

Happy path + auth failure + validation failure per endpoint. Needs the
disposable test DB (schema marker) + fakeredis."""

import uuid
from typing import Any

import pytest

from tests.conftest import run_async

pytestmark = pytest.mark.schema


def _project(api: dict[str, Any]) -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    resp = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


# --- messages --------------------------------------------------------------


def test_post_message_derives_sender_and_mints_thread(api: dict[str, Any]) -> None:
    pid = _project(api)
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "human", "body": {"text": "please start"}},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Sender is the authenticated user (never client input), and a thread is minted.
    assert body["sender_user_id"] == str(api["user_ids"]["owner"])
    assert body["sender_agent_id"] is None
    assert uuid.UUID(body["thread_id"])
    assert body["body"] == {"text": "please start"}


def test_list_messages_filters_thread_and_kind(api: dict[str, Any]) -> None:
    pid = _project(api)
    client, headers = api["client"], api["token_for"]("owner")
    thread = str(uuid.uuid4())
    client.post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "progress_report", "body": {"n": 1}, "thread_id": thread},
        headers=headers,
    )
    client.post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "human", "body": {"n": 2}},
        headers=headers,
    )
    by_thread = client.get(
        f"/api/v1/projects/{pid}/messages?thread_id={thread}", headers=headers
    ).json()
    assert len(by_thread["items"]) == 1 and by_thread["items"][0]["thread_id"] == thread

    by_kind = client.get(
        f"/api/v1/projects/{pid}/messages?kind=progress_report", headers=headers
    ).json()
    assert all(m["kind"] == "progress_report" for m in by_kind["items"])


def test_post_message_invalid_kind_422(api: dict[str, Any]) -> None:
    pid = _project(api)
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "gossip", "body": {}},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 422


def test_post_message_forbidden_for_viewer(api: dict[str, Any]) -> None:
    pid = _project(api)
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "human", "body": {}},
        headers=api["token_for"]("viewer"),  # viewer lacks message:write
    )
    assert resp.status_code == 403


def test_list_messages_unauthenticated_401(api: dict[str, Any]) -> None:
    pid = _project(api)
    assert api["client"].get(f"/api/v1/projects/{pid}/messages").status_code == 401


def _task(api: dict[str, Any], project_id: str) -> str:
    resp = api["client"].post(
        f"/api/v1/projects/{project_id}/tasks",
        json={"title": "t"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def test_message_links_same_project_task(api: dict[str, Any]) -> None:
    pid = _project(api)
    tid = _task(api, pid)
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "task_assignment", "body": {}, "task_id": tid},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201 and resp.json()["task_id"] == tid


def test_message_cross_project_task_422(api: dict[str, Any]) -> None:
    """A task_id from another project is rejected 422 (mirrors the decisions
    supersedes_id same-project check) — no cross-project references."""
    pid_a, pid_b = _project(api), _project(api)
    tid_b = _task(api, pid_b)
    resp = api["client"].post(
        f"/api/v1/projects/{pid_a}/messages",
        json={"kind": "human", "body": {}, "task_id": tid_b},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 422


# --- decisions -------------------------------------------------------------


def _decision_body(**over: Any) -> dict[str, Any]:
    base = {
        "title": "Use RS256 JWTs",
        "context": "We need asymmetric verification.",
        "decision": "Adopt RS256.",
        "alternatives": "HS256.",
        "consequences": "Key management overhead.",
    }
    base.update(over)
    return base


def test_post_decision_201_default_accepted(api: dict[str, Any]) -> None:
    pid = _project(api)
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/decisions",
        json=_decision_body(),
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201 and resp.json()["status"] == "accepted"


def test_superseding_decision_flips_prior_to_superseded(api: dict[str, Any]) -> None:
    pid = _project(api)
    client, headers = api["client"], api["token_for"]("owner")
    first = client.post(
        f"/api/v1/projects/{pid}/decisions", json=_decision_body(title="v1"), headers=headers
    ).json()

    resp = client.post(
        f"/api/v1/projects/{pid}/decisions",
        json=_decision_body(title="v2", supersedes_id=first["id"]),
        headers=headers,
    )
    assert resp.status_code == 201
    second = resp.json()
    assert second["supersedes_id"] == first["id"]

    listed = client.get(f"/api/v1/projects/{pid}/decisions?limit=100", headers=headers).json()
    by_id = {d["id"]: d for d in listed["items"]}
    assert by_id[first["id"]]["status"] == "superseded"
    assert by_id[second["id"]]["status"] == "accepted"


def test_decision_supersedes_unknown_is_422(api: dict[str, Any]) -> None:
    pid = _project(api)
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/decisions",
        json=_decision_body(supersedes_id=str(uuid.uuid4())),
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 422


def test_post_decision_forbidden_for_operator(api: dict[str, Any]) -> None:
    pid = _project(api)
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/decisions",
        json=_decision_body(),
        headers=api["token_for"]("operator"),  # operator lacks decision:write
    )
    assert resp.status_code == 403


def test_post_decision_validation_fail(api: dict[str, Any]) -> None:
    pid = _project(api)
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/decisions",
        json={"title": "only a title"},  # missing required ADR fields
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 422


# --- notifications ---------------------------------------------------------


def _seed_notification(api: dict[str, Any], role: str = "owner") -> str:
    from titan.messaging.service import create_notification

    async def seed() -> Any:
        async with api["engine"].begin() as conn:
            return await create_notification(
                conn,
                user_id=api["user_ids"][role],
                category="approval",
                severity="high",
                title="Approve the release",
                body="Task 42 awaits your approval.",
                link="/projects/x/board",
            )

    return str(run_async(seed).id)


def test_list_and_unread_filter(api: dict[str, Any]) -> None:
    nid = _seed_notification(api)
    client, headers = api["client"], api["token_for"]("owner")

    all_resp = client.get("/api/v1/notifications", headers=headers).json()
    assert nid in {n["id"] for n in all_resp["items"]}
    unread = client.get("/api/v1/notifications?unread=true", headers=headers).json()
    assert nid in {n["id"] for n in unread["items"]}


def test_mark_read_and_mark_all_read(api: dict[str, Any]) -> None:
    nid = _seed_notification(api)
    _seed_notification(api)
    client, headers = api["client"], api["token_for"]("owner")

    marked = client.post(f"/api/v1/notifications/{nid}:mark-read", headers=headers)
    assert marked.status_code == 200 and marked.json()["read_at"] is not None

    all_read = client.post("/api/v1/notifications:mark-all-read", headers=headers)
    assert all_read.status_code == 200 and all_read.json()["marked_read"] >= 1
    # Nothing unread remains.
    unread = client.get("/api/v1/notifications?unread=true", headers=headers).json()
    assert unread["items"] == []


def test_cannot_mark_another_users_notification(api: dict[str, Any]) -> None:
    """A notification belonging to another user is 404 (never leaked)."""
    nid = _seed_notification(api, role="owner")
    resp = api["client"].post(
        f"/api/v1/notifications/{nid}:mark-read", headers=api["token_for"]("viewer")
    )
    assert resp.status_code == 404


def test_read_state_persists_across_restart(api: dict[str, Any]) -> None:
    """DoD (TITAN-FRT-038): mark a notification read, 'restart' the app against
    the same DB, and the unread count still reflects the read state."""
    nid = _seed_notification(api)
    headers = api["token_for"]("owner")
    assert (
        api["client"].post(f"/api/v1/notifications/{nid}:mark-read", headers=headers).status_code
        == 200
    )

    # A brand-new app instance on the same database — a process restart.
    with api["new_client"]() as fresh:
        unread = fresh.get("/api/v1/notifications?unread=true", headers=headers).json()
        assert nid not in {n["id"] for n in unread["items"]}
        one = fresh.get("/api/v1/notifications", headers=headers).json()
        persisted = {n["id"]: n for n in one["items"]}[nid]
        assert persisted["read_at"] is not None
