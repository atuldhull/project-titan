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


def test_list_messages_filters_by_task(api: dict[str, Any]) -> None:
    """Phase 59 / Vol 4 §6.3: the task Sheet's conversation AND its event history.

    `messages.task_id` has been a real FK since 0003 and every transition writes
    one (`orchestrator.service._write_transition_message`) — the data was durable
    all along, just unreadable without this filter."""
    owner = api["token_for"]("owner")
    pid = _project(api)
    tid = str(
        api["client"]
        .post(f"/api/v1/projects/{pid}/tasks", json={"title": "t"}, headers=owner)
        .json()["id"]
    )
    other = str(
        api["client"]
        .post(f"/api/v1/projects/{pid}/tasks", json={"title": "other"}, headers=owner)
        .json()["id"]
    )
    # Project chatter with no task at all — must never leak into a task's thread.
    api["client"].post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "human", "body": {"text": "unrelated"}},
        headers=owner,
    )
    api["client"].post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "human", "body": {"text": "about other"}, "task_id": other},
        headers=owner,
    )
    # backlog -> ready is the SYSTEM transition: its message_kind is None, so it
    # writes NOTHING. This is the documented hole in the history — pinned here so
    # no Sheet is ever built assuming every move leaves a trace.
    assert (
        api["client"]
        .post(f"/api/v1/tasks/{tid}:transition", json={"action": "ready"}, headers=owner)
        .status_code
        == 200
    )
    after_system_move = api["client"].get(
        f"/api/v1/projects/{pid}/messages?task_id={tid}", headers=owner
    )
    assert after_system_move.json()["items"] == []

    # ready -> blocked DOES carry a kind: the history entry the Sheet renders.
    assert (
        api["client"]
        .post(f"/api/v1/tasks/{tid}:transition", json={"action": "block"}, headers=owner)
        .status_code
        == 200
    )

    r = api["client"].get(f"/api/v1/projects/{pid}/messages?task_id={tid}", headers=owner)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert {i["task_id"] for i in items} == {tid}
    # What happened is body.action — `kind` is lossy (block/start/merge/fail all
    # collapse onto progress_report), so a Sheet reading `kind` would misreport.
    assert [i["body"]["action"] for i in items] == ["block"]
    assert items[0]["kind"] == "progress_report"
    assert items[0]["body"]["from"] == "ready"
    assert items[0]["body"]["to"] == "blocked"

    # Unfiltered still returns everything (the filter is opt-in, not a default).
    everything = api["client"].get(f"/api/v1/projects/{pid}/messages", headers=owner).json()
    assert len(everything["items"]) == 3


def test_list_messages_task_filter_composes_with_kind(api: dict[str, Any]) -> None:
    owner = api["token_for"]("owner")
    pid = _project(api)
    tid = str(
        api["client"]
        .post(f"/api/v1/projects/{pid}/tasks", json={"title": "t"}, headers=owner)
        .json()["id"]
    )
    api["client"].post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "human", "body": {"text": "hi"}, "task_id": tid},
        headers=owner,
    )
    api["client"].post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "bug_report", "body": {"text": "bug"}, "task_id": tid},
        headers=owner,
    )
    # A message matching the KIND but on a DIFFERENT task. Without it the kind
    # filter alone yields the asserted result and the test proves nothing about
    # composition — it would pass against a build with no task filter at all.
    other = str(
        api["client"]
        .post(f"/api/v1/projects/{pid}/tasks", json={"title": "other"}, headers=owner)
        .json()["id"]
    )
    api["client"].post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "bug_report", "body": {"text": "other bug"}, "task_id": other},
        headers=owner,
    )

    r = api["client"].get(
        f"/api/v1/projects/{pid}/messages?task_id={tid}&kind=bug_report", headers=owner
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["kind"] for i in items] == ["bug_report"]
    assert [i["body"]["text"] for i in items] == ["bug"]  # not "other bug"

    # Both predicates are load-bearing: kind alone would return two.
    kind_only = api["client"].get(f"/api/v1/projects/{pid}/messages?kind=bug_report", headers=owner)
    assert len(kind_only.json()["items"]) == 2


# --- ?sender_agent_id: one agent's own output (Phase 61; Vol 4 §6.5 drawer) --


def _agent(api: dict[str, Any], project_id: str, name: str) -> str:
    resp = api["client"].post(
        "/api/v1/agents",
        json={
            # `backend`, not the roster's `eng_backend`: the agents table and
            # the YAML roster use DIFFERENT role vocabularies, bridged by
            # definition.ROLE_TO_ROSTER.
            "role": "backend",
            "name": name,
            "project_id": project_id,
            "model_binding": {"provider": "ollama", "model": "qwen2.5-coder:7b"},
        },
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _assigned_task(api: dict[str, Any], project_id: str, agent_id: str) -> str:
    """Drive a real task through ready -> assigned by `agent_id`.

    Deliberately routed through the transition endpoint rather than posting a
    message: `assign` is the genuine path that writes a message with
    sender_agent_id set (`_write_transition_message` passes actor_agent_id
    straight through), so this proves the filter against the log agents really
    produce. A message POSTed over REST by a human can never carry a
    sender_agent_id at all — the sender is derived from the principal.
    """
    client, headers = api["client"], api["token_for"]("owner")
    task = client.post(
        f"/api/v1/projects/{project_id}/tasks",
        json={"title": f"work for {agent_id}"},
        headers=headers,
    )
    assert task.status_code == 201, task.text
    tid = str(task.json()["id"])
    # backlog -> ready is the action `ready`, and it writes NO message
    # (message_kind is None — the scheduler drives it). The assign below is what
    # puts an agent-sent row in the log.
    ready = client.post(
        f"/api/v1/tasks/{tid}:transition", json={"action": "ready"}, headers=headers
    )
    assert ready.status_code == 200, ready.text
    assigned = client.post(
        f"/api/v1/tasks/{tid}:transition",
        json={"action": "assign", "actor_agent_id": agent_id},
        headers=headers,
    )
    assert assigned.status_code == 200, assigned.text
    return tid


def test_list_messages_filters_by_sender_agent(api: dict[str, Any]) -> None:
    pid = _project(api)
    mine, theirs = _agent(api, pid, "be-1"), _agent(api, pid, "be-2")
    my_task = _assigned_task(api, pid, mine)
    _assigned_task(api, pid, theirs)

    resp = api["client"].get(
        f"/api/v1/projects/{pid}/messages",
        params={"sender_agent_id": mine},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items, "the assign transition should have written a message"
    assert {m["sender_agent_id"] for m in items} == {mine}
    assert {m["task_id"] for m in items} == {my_task}


def test_sender_agent_filter_excludes_human_messages(api: dict[str, Any]) -> None:
    """A human's message carries sender_user_id, never sender_agent_id — so an
    agent filter must not surface it. This is why the index is partial."""
    pid = _project(api)
    agent = _agent(api, pid, "be-1")
    _assigned_task(api, pid, agent)
    api["client"].post(
        f"/api/v1/projects/{pid}/messages",
        json={"kind": "human", "body": {"text": "top priority please"}},
        headers=api["token_for"]("owner"),
    )

    unfiltered = (
        api["client"]
        .get(f"/api/v1/projects/{pid}/messages", headers=api["token_for"]("owner"))
        .json()["items"]
    )
    assert any(m["kind"] == "human" for m in unfiltered), "the human message is in the log"

    filtered = (
        api["client"]
        .get(
            f"/api/v1/projects/{pid}/messages",
            params={"sender_agent_id": agent},
            headers=api["token_for"]("owner"),
        )
        .json()["items"]
    )
    assert filtered, "the agent's own message survives the filter"
    assert all(m["kind"] != "human" for m in filtered)
    assert all(m["sender_user_id"] is None for m in filtered)


def test_sender_agent_filter_composes_with_kind(api: dict[str, Any]) -> None:
    pid = _project(api)
    agent, other = _agent(api, pid, "be-1"), _agent(api, pid, "be-2")
    _assigned_task(api, pid, agent)
    # A SECOND agent with a task_assignment of its own: without it the kind
    # filter alone would satisfy the assertions and the sender filter could be
    # deleted with this test still green.
    _assigned_task(api, pid, other)

    resp = api["client"].get(
        f"/api/v1/projects/{pid}/messages",
        params={"sender_agent_id": agent, "kind": "task_assignment"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items and all(
        m["kind"] == "task_assignment" and m["sender_agent_id"] == agent for m in items
    )

    empty = api["client"].get(
        f"/api/v1/projects/{pid}/messages",
        params={"sender_agent_id": agent, "kind": "bug_report"},
        headers=api["token_for"]("owner"),
    )
    assert empty.status_code == 200 and empty.json()["items"] == []


def _bus_message(api: dict[str, Any], project_id: str, from_agent: str, to_agent: str) -> None:
    """Emit a message the way an AGENT really does — through the bus.

    This is the path that matters and the one a REST-only test cannot reach.
    `bus.send` writes `sender_agent_id=None` on purpose ("bus messages identify
    their sender by role") and records the sender as a ROLE in `from_agent`. So
    every message an agent emits during a turn is invisible to a
    `?sender_agent_id=` filter — which is exactly the hole this pins.
    """
    from titan.messaging import bus
    from titan.messaging.protocol import AgentMessage

    async def send() -> None:
        async with api["engine"].begin() as conn:
            await bus.send(
                conn,
                AgentMessage(
                    type="ProgressReport",
                    project_id=uuid.UUID(project_id),
                    from_agent=from_agent,
                    to_agent=to_agent,
                    payload={"status": "in_progress", "percent_complete": 60, "summary": "wip"},
                ),
                actor=from_agent,
            )

    run_async(send)


def test_bus_messages_carry_no_sender_agent_id_only_a_role(api: dict[str, Any]) -> None:
    """The fact the whole agent-detail view depends on.

    An agent's own output is attributed by ROLE, never by agent id. A drawer
    that asked `?sender_agent_id=` for "this agent's messages" would render an
    empty list next to an agent that has been talking all day.
    """
    pid = _project(api)
    _bus_message(api, pid, from_agent="eng_backend", to_agent="qa")

    listed = (
        api["client"]
        .get(f"/api/v1/projects/{pid}/messages", headers=api["token_for"]("owner"))
        .json()["items"]
    )
    assert len(listed) == 1
    assert listed[0]["sender_agent_id"] is None
    assert listed[0]["from_agent"] == "eng_backend"


def test_list_messages_filters_by_from_agent(api: dict[str, Any]) -> None:
    pid = _project(api)
    _bus_message(api, pid, from_agent="eng_backend", to_agent="qa")
    _bus_message(api, pid, from_agent="qa", to_agent="eng_backend")

    resp = api["client"].get(
        f"/api/v1/projects/{pid}/messages",
        params={"from_agent": "eng_backend"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["from_agent"] == "eng_backend"


def test_sender_agent_id_does_not_see_bus_output(api: dict[str, Any]) -> None:
    """Pins the LIMIT of the sender_agent_id filter rather than routing around it.

    An agent with both an actor-driven transition message AND real bus output:
    the id filter finds only the former. Anything claiming to show "everything
    this agent said" must use from_agent.
    """
    pid = _project(api)
    agent = _agent(api, pid, "be-1")
    _assigned_task(api, pid, agent)  # writes sender_agent_id = agent
    _bus_message(api, pid, from_agent="eng_backend", to_agent="qa")  # writes from_agent

    by_id = (
        api["client"]
        .get(
            f"/api/v1/projects/{pid}/messages",
            params={"sender_agent_id": agent},
            headers=api["token_for"]("owner"),
        )
        .json()["items"]
    )
    by_role = (
        api["client"]
        .get(
            f"/api/v1/projects/{pid}/messages",
            params={"from_agent": "eng_backend"},
            headers=api["token_for"]("owner"),
        )
        .json()["items"]
    )

    assert {m["kind"] for m in by_id} == {"task_assignment"}
    assert {m["kind"] for m in by_role} == {"progress_report"}
    # Disjoint: the two filters see different halves of the same agent's life.
    assert {m["id"] for m in by_id}.isdisjoint({m["id"] for m in by_role})
