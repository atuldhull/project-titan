"""Derived artifacts: regeneration, versioning, replay determinism, and export
(Phase 51; Vol 6 §5, TITAN-WFL-020). Schema-marked — needs the test DB. Seeds a
project across domains, then proves the WFL-020 acceptance: deleting every artifact
row and regenerating reproduces byte-identical content."""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.artifacts import service as artifacts
from titan.debt import service as debt
from titan.planning import service as planning

pytestmark = pytest.mark.schema

_BRIEF = {
    "artifact_type": "ProjectBrief",
    "vision": "A local-first CRM.",
    "target_users": ["sales"],
    "features": [
        {"id": "F1", "name": "Contacts", "moscow": "must", "success_criterion": "add<1s"},
        {"id": "F2", "name": "Search", "moscow": "should", "success_criterion": "fast"},
    ],
    "non_goals": ["no telephony"],
    "constraints": {"canonical_stack": True},
    "success_criteria": ["add a contact in < 1s"],
    "assumptions": [],
}
_PLAN = {
    "artifact_type": "SprintPlan",
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
_ARCH = {
    "artifact_type": "ArchitecturePackage",
    "component_diagram_ascii": "[r]->[uc]->[e]",
    "components": [
        {"name": "ContactEntity", "layer": "entities", "depends_on": []},
        {"name": "AddContact", "layer": "use_cases", "depends_on": ["ContactEntity"]},
    ],
    "api_contracts": [],
    "database": {"tables": [], "migration_order": []},
    "frontend": {"routes": [], "state_management": "redux"},
    "adrs": [],
    "task_components": [{"task_id": "T1", "component": "AddContact"}],
}


async def _insert_handoff(
    engine: Any, pid: uuid.UUID, stage: str, atype: str, body: dict[str, Any]
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO handoff_artifacts (id, project_id, stage, artifact_type, "
                "schema_version, version, body, submitted_by) "
                "VALUES (:id, :pid, :st, :at, 1, 1, CAST(:body AS jsonb), 'agent.test')"
            ),
            {"id": uuid.uuid4(), "pid": pid, "st": stage, "at": atype, "body": json.dumps(body)},
        )


async def _seed(engine: Any) -> uuid.UUID:
    """A project with cross-domain state feeding all six artifacts."""
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'c', 'local', :s)"
            ),
            {"id": uid, "e": f"u-{uid.hex[:12]}@t.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :n, 'P', 'b', :u)"
            ),
            {"id": pid, "n": f"p-{pid.hex[:12]}", "u": uid},
        )
        for tid, title, status in [(t1, "add contact", "done"), (t2, "search", "ready")]:
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, project_id, title, status, priority) "
                    "VALUES (:id, :pid, :t, :st, 2)"
                ),
                {"id": tid, "pid": pid, "t": title, "st": status},
            )
        await conn.execute(
            text("INSERT INTO task_dependencies (task_id, depends_on_id) VALUES (:t, :d)"),
            {"t": t2, "d": t1},
        )
        await conn.execute(
            text(
                "INSERT INTO deployments (id, project_id, name, compose_file, status, health, url) "
                "VALUES (:id, :pid, 'web', 'docker-compose.yml', 'running', 'green', 'http://localhost:8080')"
            ),
            {"id": uuid.uuid4(), "pid": pid},
        )
        await conn.execute(
            text(
                "INSERT INTO task_blocks (id, project_id, task_id, reason, rung, status) "
                "VALUES (:id, :pid, :tid, 'stalled', 'dependency_recheck', 'open')"
            ),
            {"id": uuid.uuid4(), "pid": pid, "tid": t2},
        )
        await debt.open_ticket(
            conn, pid, source="security", title="weak hash", fingerprint="sec-1", detail="md5"
        )
        await debt.open_ticket(
            conn, pid, source="code_review", title="dup util", fingerprint="cr-1", detail="dup"
        )
    for stage, atype, body in [
        ("ceo_vision", "ProjectBrief", _BRIEF),
        ("pmgr_plan", "SprintPlan", _PLAN),
        ("architect_design", "ArchitecturePackage", _ARCH),
    ]:
        await _insert_handoff(engine, pid, stage, atype, body)
    # Milestone M2.
    await planning.sync_milestone(
        engine,
        pid,
        planning.MilestoneSignalsIn(
            brief_accepted=True,
            backlog_accepted=True,
            skeleton_deployed=True,
            hello_path_green=True,
            must_stories_done=True,
            g6_g7_passed=True,
        ),
        actor="agent.pm",
    )
    return pid


def test_regenerate_produces_all_six_and_is_idempotent(migrated_engine: Any) -> None:
    pid = run_async(lambda: _seed(migrated_engine))
    first = run_async(lambda: artifacts.regenerate_all(migrated_engine, pid))
    assert {a.kind for a in first} == set(artifacts.ARTIFACT_KINDS)
    assert all(a.artifact_version == 1 for a in first)
    # Non-empty content on each.
    assert all(a.content_md.strip() and a.content_json for a in first)

    # A second regeneration with unchanged domain state versions nothing (idempotent).
    second = run_async(lambda: artifacts.regenerate_all(migrated_engine, pid))
    assert all(a.artifact_version == 1 for a in second)
    assert {a.kind: a.content_hash for a in first} == {a.kind: a.content_hash for a in second}


def test_replay_reproduces_byte_identical_artifacts(migrated_engine: Any) -> None:
    """TITAN-WFL-020: delete all artifact rows, regenerate, and the content is
    byte-identical."""
    pid = run_async(lambda: _seed(migrated_engine))
    before = {a.kind: a for a in run_async(lambda: artifacts.regenerate_all(migrated_engine, pid))}

    removed = run_async(lambda: artifacts.reset_project_artifacts(migrated_engine, pid))
    assert removed == len(artifacts.ARTIFACT_KINDS)

    after = {a.kind: a for a in run_async(lambda: artifacts.regenerate_all(migrated_engine, pid))}
    for kind in artifacts.ARTIFACT_KINDS:
        assert after[kind].content_hash == before[kind].content_hash, kind
        assert after[kind].content_md == before[kind].content_md, kind
        assert after[kind].content_json == before[kind].content_json, kind


def test_content_reflects_seeded_state(migrated_engine: Any) -> None:
    pid = run_async(lambda: _seed(migrated_engine))
    arts = {a.kind: a for a in run_async(lambda: artifacts.regenerate_all(migrated_engine, pid))}
    # Roadmap: milestone M2 reached, MUST feature first.
    assert arts["roadmap"].content_json["current_milestone"] == "M2"
    assert arts["roadmap"].content_json["priorities"][0]["feature_id"] == "F1"
    # Dependency graph: the T2->T1 edge + the arch components.
    dg = arts["dependency_graph"].content_json
    assert {"task_id", "depends_on_id"} <= set(dg["tasks"]["edges"][0])
    assert any(n["name"] == "AddContact" for n in dg["components"]["nodes"])
    # Debt report: two tickets.
    assert arts["debt_report"].content_json["open_total"] == 2
    # Risk analysis: the open block + the security medium + is ranked.
    cats = {r["category"] for r in arts["risk_analysis"].content_json["risks"]}
    assert "blocker" in cats and "security" in cats


def test_regenerate_versions_on_a_content_change(migrated_engine: Any) -> None:
    pid = run_async(lambda: _seed(migrated_engine))
    run_async(lambda: artifacts.regenerate_all(migrated_engine, pid))
    # Add a debt ticket → the debt report content changes → a new version.
    run_async(lambda: _add_debt(migrated_engine, pid))
    updated = run_async(lambda: artifacts.regenerate_artifact(migrated_engine, pid, "debt_report"))
    assert updated.artifact_version == 2
    assert updated.content_json["open_total"] == 3


async def _add_debt(engine: Any, pid: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await debt.open_ticket(
            conn, pid, source="constitution", title="new debt", fingerprint="c-1", detail="x"
        )


# --- export API --------------------------------------------------------------


def _project_via_api(api: dict[str, Any]) -> uuid.UUID:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    resp = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(str(resp.json()["id"]))


def test_export_api_regenerate_list_get_and_export(api: dict[str, Any]) -> None:
    pid = _project_via_api(api)
    hdr = api["token_for"]("owner")

    regen = api["client"].post(f"/api/v1/projects/{pid}/artifacts:regenerate", headers=hdr)
    assert regen.status_code == 200, regen.text
    assert {a["kind"] for a in regen.json()} == set(artifacts.ARTIFACT_KINDS)

    listed = api["client"].get(f"/api/v1/projects/{pid}/artifacts", headers=hdr)
    assert listed.status_code == 200 and len(listed.json()) == 6

    got = api["client"].get(f"/api/v1/projects/{pid}/artifacts/roadmap", headers=hdr)
    assert got.status_code == 200
    assert got.json()["kind"] == "roadmap" and "# Roadmap" in got.json()["content_md"]

    md = api["client"].get(
        f"/api/v1/projects/{pid}/artifacts/roadmap/export?format=md", headers=hdr
    )
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    js = api["client"].get(
        f"/api/v1/projects/{pid}/artifacts/kanban/export?format=json", headers=hdr
    )
    assert js.status_code == 200 and js.headers["content-type"].startswith("application/json")

    # Unknown kind → 404; unread role → 403.
    assert (
        api["client"].get(f"/api/v1/projects/{pid}/artifacts/bogus", headers=hdr).status_code == 404
    )
