"""Planning REST surface (Phase 50; Vol 6 §4). Exercises the sprint-plan preview,
sprint ceremonies, milestone sync, and blocker sweep endpoints through the ASGI app
with real RBAC (the ``pipeline:advance``/``pipeline:read`` PM role)."""

import uuid
from typing import Any


def _project(api: dict[str, Any]) -> uuid.UUID:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    resp = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(str(resp.json()["id"]))


_BACKLOG = {
    "tasks": [
        {"id": "db", "role": "database", "priority": 1},
        {"id": "be", "role": "backend", "priority": 1},
        {"id": "fe", "role": "frontend", "priority": 1},
        {"id": "deploy", "role": "devops", "priority": 1},
        {"id": "feat", "role": "backend", "priority": 2},
    ],
    "edges": [
        {"task_id": "be", "depends_on_id": "db"},
        {"task_id": "fe", "depends_on_id": "be"},
        {"task_id": "deploy", "depends_on_id": "fe"},
        {"task_id": "feat", "depends_on_id": "be"},
    ],
    "parallel_capacity": 4,
}


def test_sprint_plan_preview_is_walking_skeleton_first(api: dict[str, Any]) -> None:
    pid = _project(api)
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/sprint-plan:preview",
        json=_BACKLOG,
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    first = next(s for s in body["sprints"] if s["number"] == 1)
    assert first["walking_skeleton"] is True
    assert set(first["task_ids"]) == {"db", "be", "fe", "deploy"}
    assert body["sprints"][-1]["number"] >= 2  # the feature task lands in a later sprint


def test_preview_rejects_a_cyclic_backlog_with_422(api: dict[str, Any]) -> None:
    pid = _project(api)
    bad = {
        "tasks": [
            {"id": "db", "role": "database"},
            {"id": "be", "role": "backend"},
            {"id": "fe", "role": "frontend"},
            {"id": "deploy", "role": "devops"},
        ],
        "edges": [
            {"task_id": "db", "depends_on_id": "be"},
            {"task_id": "be", "depends_on_id": "db"},
        ],
    }
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/sprint-plan:preview", json=bad, headers=api["token_for"]("owner")
    )
    assert resp.status_code == 422, resp.text


def test_sprint_ceremonies_open_close_and_list(api: dict[str, Any]) -> None:
    pid = _project(api)
    hdr = api["token_for"]("owner")
    opened = api["client"].post(
        f"/api/v1/projects/{pid}/sprints:open",
        json={"sprint_number": 1, "goal": "skeleton", "committed_stories": ["S1"], "risks": []},
        headers=hdr,
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["kind"] == "planning"

    closed = api["client"].post(
        f"/api/v1/projects/{pid}/sprints:close",
        json={
            "sprint_number": 1,
            "demo_links": ["http://demo"],
            "velocity_estimate": 8,
            "velocity_actual": 10,
            "retrospective_changes": ["smaller tasks"],
            "estimate_actuals": [
                {"estimate": 2, "actual": 3},
                {"estimate": 3, "actual": 3},
                {"estimate": 5, "actual": 5},
            ],
        },
        headers=hdr,
    )
    assert closed.status_code == 200, closed.text
    assert "correction_factor" in closed.json()

    listed = api["client"].get(f"/api/v1/projects/{pid}/ceremonies", headers=hdr)
    assert listed.status_code == 200, listed.text
    assert {c["kind"] for c in listed.json()} == {"planning", "review", "retrospective"}


def test_milestone_sync_and_read(api: dict[str, Any]) -> None:
    pid = _project(api)
    hdr = api["token_for"]("owner")
    synced = api["client"].post(
        f"/api/v1/projects/{pid}/milestone:sync",
        json={"brief_accepted": True, "backlog_accepted": True},
        headers=hdr,
    )
    assert synced.status_code == 200, synced.text
    assert synced.json()["current"] == "M0"
    got = api["client"].get(f"/api/v1/projects/{pid}/milestone", headers=hdr)
    assert got.status_code == 200 and got.json()["current_milestone"] == "M0"


def test_blocker_sweep_endpoint_on_empty_project(api: dict[str, Any]) -> None:
    pid = _project(api)
    hdr = api["token_for"]("owner")
    swept = api["client"].post(f"/api/v1/projects/{pid}/blockers:sweep", headers=hdr)
    assert swept.status_code == 200, swept.text
    assert swept.json()["detected"] == 0
    listed = api["client"].get(f"/api/v1/projects/{pid}/blockers", headers=hdr)
    assert listed.status_code == 200 and listed.json() == []


def test_planning_writes_require_pipeline_advance(api: dict[str, Any]) -> None:
    """A read-only 'viewer' cannot open a sprint (needs pipeline:advance)."""
    pid = _project(api)
    resp = api["client"].post(
        f"/api/v1/projects/{pid}/sprints:open",
        json={"sprint_number": 1, "goal": "x"},
        headers=api["token_for"]("viewer"),
    )
    assert resp.status_code == 403, resp.text
