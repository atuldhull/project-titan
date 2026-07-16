"""The published task workflow definition (Phase 59; Vol 4 §6.3).

The Kanban board's columns and its legal drags ARE the Phase-41 transition
table. Publishing it means a client stops hardcoding a copy — so these tests pin
the property that makes that safe: what `/api/v1/workflow` says is exactly what
`transitions.py` holds and what `apply_transition` enforces, with no third
place to drift.
"""

import uuid
from typing import Any

import pytest

from titan.orchestrator import service, transitions

pytestmark = pytest.mark.schema


# --- pure: the projection is faithful to the table --------------------------


def test_workflow_publishes_every_transition_and_nothing_else() -> None:
    """Exhaustive: the endpoint's edge set IS the table's. A board refuses any
    (from, to) pair absent here, so a missing edge silently disables a real move
    and an invented one produces a guaranteed 409."""
    definition = service.workflow_definition()
    published = {(t.from_status, t.action) for t in definition.transitions}
    assert published == set(transitions.TRANSITIONS)

    by_key = {(t.from_status, t.action): t for t in definition.transitions}
    for key, expected in transitions.TRANSITIONS.items():
        got = by_key[key]
        assert got.to_status == expected.to_status
        assert got.required_permission == expected.required_permission
        assert got.requires_actor_agent == expected.requires_actor_agent


def test_workflow_states_are_the_canonical_board_order() -> None:
    """`states` is the board's column order — it must be STATES verbatim, not
    sorted: `backlog` leads and `done` follows `changes_requested`. Alphabetising
    would silently reorder every board."""
    assert service.workflow_definition().states == list(transitions.STATES)
    assert service.workflow_definition().states[0] == "backlog"


def test_only_assign_and_the_review_actions_require_an_actor_agent() -> None:
    """The flag is what `apply_transition` raises its 409 from AND what the board
    collects an agent for. Pin the set: flagging a new action changes UI flow, and
    unflagging one turns a pre-collected agent into a dropped assignee."""
    flagged = {t.action for t in transitions.TRANSITIONS.values() if t.requires_actor_agent}
    assert flagged == {"assign", "approve", "request_changes"}


def test_actor_requiring_actions_all_have_a_409_detail() -> None:
    """Every flagged action needs a message saying what the agent is FOR; the
    lookup falls back rather than KeyError-ing into a 500, so this is what keeps
    the fallback from ever being reached."""
    flagged = {t.action for t in transitions.TRANSITIONS.values() if t.requires_actor_agent}
    assert flagged <= set(service._ACTOR_REQUIRED_DETAIL)


# --- the endpoint -----------------------------------------------------------


def test_get_workflow_returns_the_machine(api: dict[str, Any]) -> None:
    r = api["client"].get("/api/v1/workflow", headers=api["token_for"]("owner"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["states"] == list(transitions.STATES)

    edges = {(t["from_status"], t["action"]): t for t in body["transitions"]}
    assert edges[("ready", "assign")]["to_status"] == "assigned"
    assert edges[("ready", "assign")]["required_permission"] == "task:assign"
    assert edges[("ready", "assign")]["requires_actor_agent"] is True
    assert edges[("backlog", "ready")]["requires_actor_agent"] is False
    # A board must NOT offer backlog -> in_progress: no single action gets there.
    assert ("backlog", "start") not in edges


def test_get_workflow_requires_authentication(api: dict[str, Any]) -> None:
    """Static metadata, but not public — it describes the machine to callers who
    are already inside the API."""
    assert api["client"].get("/api/v1/workflow").status_code == 401


def test_viewer_can_read_the_workflow(api: dict[str, Any]) -> None:
    """A viewer renders the board (read-only) and so must know its columns, even
    though `task:transition` will refuse every drag."""
    r = api["client"].get("/api/v1/workflow", headers=api["token_for"]("viewer"))
    assert r.status_code == 200, r.text


# --- the board card carries points (Vol 4 §6.3 renders them) ----------------


def test_board_card_carries_estimate_points(api: dict[str, Any]) -> None:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    owner = api["token_for"]("owner")
    pid = (
        api["client"]
        .post(
            "/api/v1/projects",
            json={"name": name, "display_name": name, "brief": "b"},
            headers=owner,
        )
        .json()["id"]
    )
    api["client"].post(
        f"/api/v1/projects/{pid}/tasks",
        json={"title": "pointed", "estimate_points": 5},
        headers=owner,
    )
    api["client"].post(f"/api/v1/projects/{pid}/tasks", json={"title": "unpointed"}, headers=owner)

    r = api["client"].get(f"/api/v1/projects/{pid}/board", headers=owner)
    assert r.status_code == 200, r.text
    cards = {c["title"]: c for col in r.json()["columns"] for c in col["tasks"]}
    assert cards["pointed"]["estimate_points"] == 5
    # Unestimated is None, never 0 — a board that renders "0 pts" for "not sized
    # yet" is stating a number nobody entered.
    assert cards["unpointed"]["estimate_points"] is None
