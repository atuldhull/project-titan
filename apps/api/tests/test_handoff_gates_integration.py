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


# --- artifact version history (Phase 60; Vol 4 §6.8, TITAN-FRT-031) ----------


def _versions(api: dict[str, Any], pid: str, stage: str) -> Any:
    return api["client"].get(
        f"/api/v1/projects/{pid}/handoffs/{stage}/artifact-versions",
        headers=api["token_for"]("owner"),
    )


def _version(api: dict[str, Any], pid: str, stage: str, version: int) -> Any:
    return api["client"].get(
        f"/api/v1/projects/{pid}/handoffs/{stage}/artifact-versions/{version}",
        headers=api["token_for"]("owner"),
    )


def test_three_architecture_revisions_are_listed_and_individually_fetchable(
    api: dict[str, Any],
) -> None:
    """TITAN-FRT-031's data requirement: every architecture version is viewable
    and diffable against any other.

    The revisions are minted by the REAL submit path — a failing G5 keeps the
    stage current, which is exactly how an Architect revises: gate objects,
    Architect resubmits, `version` increments. Each body is preserved, never
    overwritten (TITAN-WFL-003); the diff a client draws is a function of two of
    them, so both must be fetchable. Before this seam only the newest was.
    """
    pid = _project(api)
    assert _submit(api, pid, "ceo_vision", BRIEF).json()["outcome"] == "advanced"
    assert _submit(api, pid, "pm_backlog", BACKLOG).json()["outcome"] == "advanced"
    assert _submit(api, pid, "pmgr_plan", PLAN).json()["outcome"] == "advanced"

    # G5 objects, so the Architect revises. Three distinct architectures.
    gates.register_gate("architect_design", gates.ConfigurableGate(passes=False, reasons=["G5"]))
    for n in (1, 2, 3):
        revision = {
            **ARCH,
            "components": [
                *ARCH["components"],
                {"name": f"Extra{n}", "layer": "frameworks", "depends_on": ["ContactRouter"]},
            ],
        }
        r = _submit(api, pid, "architect_design", revision)
        assert r.status_code == 200, r.text
        assert r.json()["attempt"] == n

    listed = _versions(api, pid, "architect_design")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [v["version"] for v in body["versions"]] == [3, 2, 1]  # newest first
    assert {v["artifact_type"] for v in body["versions"]} == {"ArchitecturePackage"}
    # Metadata only: a timeline draws version numbers, not 500-component bodies.
    assert all("body" not in v for v in body["versions"])

    # Each revision is fetchable IN FULL, and they genuinely differ — this is
    # what a diff consumes.
    v1 = _version(api, pid, "architect_design", 1)
    v3 = _version(api, pid, "architect_design", 3)
    assert v1.status_code == 200 and v3.status_code == 200
    names_v1 = {c["name"] for c in v1.json()["body"]["components"]}
    names_v3 = {c["name"] for c in v3.json()["body"]["components"]}
    assert "Extra1" in names_v1 and "Extra1" not in names_v3
    assert "Extra3" in names_v3 and "Extra3" not in names_v1

    # ...and `../artifact` still returns the newest, unchanged.
    latest = api["client"].get(
        f"/api/v1/projects/{pid}/handoffs/architect_design/artifact",
        headers=api["token_for"]("owner"),
    )
    assert latest.json()["version"] == 3


def test_artifact_versions_is_empty_not_404_before_any_submission(api: dict[str, Any]) -> None:
    """A stage with no artifact yet has an empty HISTORY, which is an answer, not
    an error — the Architecture screen's "not yet designed" state reads this."""
    pid = _project(api)
    r = _versions(api, pid, "architect_design")
    assert r.status_code == 200, r.text
    assert r.json()["versions"] == []
    assert r.json()["stage"] == "architect_design"


def test_artifact_version_unknown_version_is_404(api: dict[str, Any]) -> None:
    pid = _project(api)
    assert _submit(api, pid, "ceo_vision", BRIEF).json()["outcome"] == "advanced"
    assert _version(api, pid, "ceo_vision", 1).status_code == 200
    assert _version(api, pid, "ceo_vision", 99).status_code == 404


def test_artifact_versions_rejects_a_non_producing_stage(api: dict[str, Any]) -> None:
    """Same 422 the latest-artifact route gives — a stage with no artifact schema
    has no history to have."""
    pid = _project(api)
    assert _versions(api, pid, "not_a_stage").status_code == 422
    assert _version(api, pid, "not_a_stage", 1).status_code == 422


def test_artifact_versions_are_project_scoped(api: dict[str, Any]) -> None:
    """One project's revisions never leak into another's timeline."""
    pid_a, pid_b = _project(api), _project(api)
    assert _submit(api, pid_a, "ceo_vision", BRIEF).json()["outcome"] == "advanced"
    assert [v["version"] for v in _versions(api, pid_a, "ceo_vision").json()["versions"]] == [1]
    assert _versions(api, pid_b, "ceo_vision").json()["versions"] == []


def test_artifact_versions_requires_read_permission(api: dict[str, Any]) -> None:
    pid = _project(api)
    assert (
        api["client"]
        .get(f"/api/v1/projects/{pid}/handoffs/architect_design/artifact-versions")
        .status_code
        == 401
    )
