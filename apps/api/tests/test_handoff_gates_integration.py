"""Phase 43 — the concrete gates G2–G5 wired through the Phase-42 executor.

Proves the §2.3 failure paths route end-to-end (a gap annotation / numbered
objection lands in the gate ledger and retries the stage) and that a G5 evaluation
records the constitution version it judged against (§2.3 H5 DoD).
"""

import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest

from titan.handoffs import gates
from titan.handoffs.standard_gates import register_standard_gates

pytestmark = pytest.mark.schema


@pytest.fixture(autouse=True)
def _standard_gates() -> Iterator[None]:
    """Register the real G2–G5 predicates for these tests (the module-global
    registry is reset afterwards so other suites keep the auto-pass default)."""
    gates.clear_gates()
    register_standard_gates()
    yield
    gates.clear_gates()


BRIEF: dict[str, Any] = {
    "vision": "A local-first CRM.",
    "target_users": ["sales"],
    "features": [{"id": "F1", "name": "Contacts", "moscow": "must", "success_criterion": "add<1s"}],
    "non_goals": ["no telephony"],
    "constraints": {"canonical_stack": True},
    "success_criteria": ["add a contact in < 1s"],
    "assumptions": [],
}
BACKLOG: dict[str, Any] = {
    "stories": [
        {
            "id": "S1",
            "as_a": "sales rep",
            "i_want": "to add a contact",
            "so_that": "I can track leads",
            "acceptance_criteria": [{"given": "a name", "when": "I save", "then": "it appears"}],
            "priority": 1,
            "points": 3,
            "covers_features": ["F1"],
        }
    ]
}
PLAN: dict[str, Any] = {
    "sprints": [{"number": 1, "goal": "skeleton", "task_ids": ["T1"], "walking_skeleton": True}],
    "tasks": [
        {
            "id": "T1",
            "title": "add contact",
            "estimate_days": 0.5,
            "assignee_role": "backend",
            "story_id": "S1",
            "sprint_number": 1,
        }
    ],
    "dependencies": [],
    "milestones": [],
}
ARCH: dict[str, Any] = {
    "component_diagram_ascii": "[router]->[uc]->[entity]",
    "components": [
        {"name": "ContactEntity", "layer": "entities", "depends_on": []},
        {"name": "AddContact", "layer": "use_cases", "depends_on": ["ContactEntity"]},
        {"name": "ContactRouter", "layer": "interface_adapters", "depends_on": ["AddContact"]},
    ],
    "api_contracts": [
        {
            "path": "/contacts",
            "method": "POST",
            "operation": {"responses": {"201": {"description": "created"}}},
        }
    ],
    "database": {
        "tables": [{"name": "contacts", "columns": ["id"], "indexes": []}],
        "migration_order": ["0001"],
    },
    "frontend": {"routes": [{"path": "/", "component": "Home"}], "state_management": "redux"},
    "adrs": [],
    "task_components": [{"task_id": "T1", "component": "AddContact"}],
}


def _project(api: dict[str, Any]) -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    r = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _submit(api: dict[str, Any], pid: str, stage: str, body: dict[str, Any]) -> Any:
    return api["client"].post(
        f"/api/v1/projects/{pid}/handoffs/{stage}", json=body, headers=api["token_for"]("owner")
    )


def _ledger(api: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    r = api["client"].get(
        f"/api/v1/projects/{pid}/handoffs/gate-ledger", headers=api["token_for"]("owner")
    )
    assert r.status_code == 200, r.text
    return cast(list[dict[str, Any]], r.json()["evaluations"])


def _active_constitution_version(api: dict[str, Any]) -> int:
    # The shared session DB may carry constitution revisions from other tests
    # (versions are append-only), so G5 records whatever is currently active.
    r = api["client"].get("/api/v1/constitution", headers=api["token_for"]("owner"))
    assert r.status_code == 200, r.text
    return int(r.json()["version"])


def test_valid_brief_passes_g2_and_advances(api: dict[str, Any]) -> None:
    pid = _project(api)
    r = _submit(api, pid, "ceo_vision", BRIEF)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "advanced" and r.json()["next_stage"] == "pm_backlog"


def test_g2_gap_annotation_routes_to_retry(api: dict[str, Any]) -> None:
    pid = _project(api)
    bad = {**BRIEF, "features": [{"id": "F1", "name": "Contacts", "moscow": "must"}]}
    r = _submit(api, pid, "ceo_vision", bad)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "gate_failed_retry" and body["attempt"] == 1
    assert any("success criterion" in x for x in body["gate_reasons"])  # gap annotation
    ledger = _ledger(api, pid)
    assert ledger[-1]["verdict"] == "fail" and ledger[-1]["evaluator_agent"] == "pm"


def test_full_flow_records_constitution_version_at_g5(api: dict[str, Any]) -> None:
    """DoD (§2.3 H5): each G5 ledger entry records the constitution version it
    judged against; the earlier gates record none."""
    pid = _project(api)
    assert _submit(api, pid, "ceo_vision", BRIEF).json()["outcome"] == "advanced"
    assert _submit(api, pid, "pm_backlog", BACKLOG).json()["outcome"] == "advanced"
    assert _submit(api, pid, "pmgr_plan", PLAN).json()["outcome"] == "advanced"
    r = _submit(api, pid, "architect_design", ARCH)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "advanced" and r.json()["next_stage"] == "engineering"

    ledger = {row["stage"]: row for row in _ledger(api, pid)}
    assert ledger["architect_design"]["verdict"] == "pass"
    # G5 records the constitution version it judged against (the active one).
    assert ledger["architect_design"]["constitution_version"] == _active_constitution_version(api)
    assert ledger["ceo_vision"]["constitution_version"] is None  # only G5 stamps it


def test_g5_objections_route_to_retry(api: dict[str, Any]) -> None:
    pid = _project(api)
    _submit(api, pid, "ceo_vision", BRIEF)
    _submit(api, pid, "pm_backlog", BACKLOG)
    _submit(api, pid, "pmgr_plan", PLAN)
    bad_arch = {**ARCH, "task_components": []}  # T1 unmapped → numbered objection
    r = _submit(api, pid, "architect_design", bad_arch)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "gate_failed_retry"
    assert any("not mapped" in x and x.startswith("1.") for x in body["gate_reasons"])
    # The failing G5 evaluation still records the constitution version it judged.
    ledger = _ledger(api, pid)
    g5 = [e for e in ledger if e["stage"] == "architect_design"][-1]
    assert g5["verdict"] == "fail"
    assert g5["constitution_version"] == _active_constitution_version(api)
