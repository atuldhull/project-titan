"""Mock-LLM end-to-end delivery (Phase 51 DoD). Schema-marked — needs the test DB.

Drives a tiny project brief through all twelve handoffs H1→H12 to a simulated
release with NO real LLM calls: clarification + high-visibility assumptions, the
gate G1–G12 chain with an induced G2 failure + retry recovery, a self-improvement
loop that converges, milestone progression M0→M4, an APPROVED_WITH_COMMENTS review
that opens debt, and a reflection + retrospective landing in organization memory —
then asserts every handoff, gate verdict, and loop iteration is queryable through the
derived-artifact and gate-ledger export APIs.
"""

import json
import uuid
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.conftest import run_async
from titan.clarification import service as clarification
from titan.debt import service as debt
from titan.handoffs import gates
from titan.handoffs import service as handoffs
from titan.handoffs.service import register_standard_gates
from titan.loop import service as loop
from titan.loop.engine import Disposition, Finding, ReviewerContext
from titan.memory import service as memory
from titan.planning import service as planning

pytestmark = pytest.mark.schema

_MB = {"provider": "anthropic", "model": "claude-x", "temperature": 0.0}
_REFLECTION_KEYS = (
    "goal",
    "what_worked",
    "what_surprised",
    "do_differently",
    "org_should_remember",
)


@pytest.fixture
def gates_registered() -> Any:
    """Register the real G1–G12 gates, overriding only G6 (engineering), whose G6
    reads execution `checks` the executor does not wire — so it fails closed through
    submit_artifact. The override still exercises G2–G5 and G7–G12 for real."""
    gates.clear_gates()
    register_standard_gates()
    gates.register_gate("engineering", gates.ConfigurableGate(passes=True))
    yield
    gates.clear_gates()


# --- the scripted mock-LLM loop driver (reviewer finds nothing → converges) --


class _CleanDriver:
    def __init__(self) -> None:
        self.reviewer_contexts: list[ReviewerContext] = []

    async def review(self, ctx: ReviewerContext, cycle: int) -> Sequence[Finding]:
        self.reviewer_contexts.append(ctx)
        return []

    async def dispose(self, finding: Finding, cycle: int) -> Disposition:
        return Disposition(kind="fixed", reason="fixed")

    async def rejudge(self, finding: Finding, disposition: Disposition, cycle: int) -> bool:
        return True

    async def stage_report(self, stage: str, cycle: int) -> tuple[bool, str]:
        return True, "ok"


# --- the twelve handoff artifact bodies (proven passing fixtures) ------------

_BAD_BRIEF = {
    "vision": "A local-first CRM.",
    "target_users": ["sales"],
    "features": [
        {"id": "F1", "name": "Contacts", "moscow": "must"}
    ],  # no success_criterion → G2 fail
    "non_goals": ["no telephony"],
    "constraints": {"canonical_stack": True},
    "success_criteria": ["add a contact in < 1s"],
    "assumptions": [],
}
_BRIEF = {
    **_BAD_BRIEF,
    "features": [{"id": "F1", "name": "Contacts", "moscow": "must", "success_criterion": "add<1s"}],
}
_BACKLOG = {
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
_PLAN = {
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
_MANIFEST = {
    "prs": [
        {
            "number": 1,
            "title": "add contact",
            "task_id": "T1",
            "branch": "feat/contact",
            "sha": "abc1234",
        }
    ],
    "changelog": "- add contact",
    "lockfiles": ["uv.lock"],
    "migrations": ["0001"],
}
_QA = {
    "test_matrix": [{"suite": "e2e", "passed": 10, "failed": 0, "total": 10}],
    "defects": [],
    "flake_log": [],
    "performance_baseline": [{"name": "start", "value": 1.2, "unit": "s"}],
}
_SEC = {
    "sast": [],
    "dependency_audit": [],
    "secret_scan": {"clean": True},
    "owasp": [{"category": "A01", "status": "pass", "evidence": "n/a"}],
    "rbac_verified": True,
}
_REVIEW = {
    "verdict": "APPROVED_WITH_COMMENTS",
    "architecture_boundary_ok": True,
    "duplication_findings": [],
    "public_api_consistent": True,
    "changelog_accurate": True,
    "comments": ["duplication in the contact util"],
}
_DEPLOY = {
    "image_digests": [{"image": "api", "digest": "sha256:x"}],
    "environment": "production",
    "applied_migrations": ["0001"],
    "smoke_tests": {"passed": 5, "failed": 0},
    "health_checks": "green",
    "rollback_pointer": "v1.0.0",
}
_DOCS = {
    "docs": [{"kind": "readme", "path": "README.md"}],
    "documented_endpoints": ["/contacts"],
    "adr_index": ["A1"],
    "quickstart_verified": True,
}
_RELEASE = {
    "approved": True,
    "summary": "ships",
    "success_criteria_met": [{"criterion": "p95<2s", "met": True}],
    "traceability": [
        {
            "feature_id": "F1",
            "story_ids": ["S1"],
            "task_ids": ["T1"],
            "pr_numbers": [1],
            "test_ids": ["e2e-1"],
            "doc_refs": ["README.md"],
        }
    ],
    "assumptions_restated": ["Payment integration is mocked"],
}

_STAGES_AFTER_BRIEF: list[tuple[str, dict[str, Any]]] = [
    ("pm_backlog", _BACKLOG),
    ("pmgr_plan", _PLAN),
    ("architect_design", _ARCH),
    ("engineering", _MANIFEST),
    ("qa", _QA),
    ("security", _SEC),
    ("code_review", _REVIEW),
    ("devops_deploy", _DEPLOY),
    ("documentation", _DOCS),
    ("ceo_release", _RELEASE),
]


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
    return run_async(
        lambda: handoffs.submit_artifact(
            api["engine"], uuid.UUID(pid), stage, body, submitted_by="agent.e2e"
        )
    )


def _transition(api: dict[str, Any], tid: str, action: str, *, actor: str | None = None) -> Any:
    body: dict[str, Any] = {"action": action}
    if actor is not None:
        body["actor_agent_id"] = actor
    return api["client"].post(
        f"/api/v1/tasks/{tid}:transition", json=body, headers=api["token_for"]("owner")
    )


def _loop_task(api: dict[str, Any], pid: str) -> str:
    r = api["client"].post(
        f"/api/v1/projects/{pid}/tasks", json={"title": "widget"}, headers=api["token_for"]("owner")
    )
    tid = str(r.json()["id"])
    aid = str(
        api["client"]
        .post(
            "/api/v1/agents",
            json={"role": "backend", "name": "bot", "model_binding": _MB},
            headers=api["token_for"]("owner"),
        )
        .json()["id"]
    )
    assert _transition(api, tid, "ready").status_code == 200
    assert _transition(api, tid, "assign", actor=aid).status_code == 200
    assert _transition(api, tid, "start").json()["status"] == "in_progress"
    return tid


class _MockGateway:
    """A mock LLM gateway whose chat() returns a canned five-field rubric, so the
    SYSTEM's reflection pipeline (generate_task_reflection → the episodic record)
    produces the memory record — not the test itself."""

    async def chat(self, request: Any) -> Any:
        return SimpleNamespace(text=json.dumps({k: f"{k} learned" for k in _REFLECTION_KEYS}))


def test_full_pipeline_to_release_is_queryable_via_export_apis(
    api: dict[str, Any], gates_registered: Any
) -> None:
    pid = _project(api)
    hdr = api["token_for"]("owner")

    # H1: clarification. Six essential questions exceed the 5-question budget, so the
    # 6th is dropped to a high-visibility best-judgment assumption the release restates.
    batch = run_async(
        lambda: clarification.submit_user_request(
            api["engine"],
            uuid.UUID(pid),
            clarification.UserRequestSubmission(
                raw_text="Build me a local-first CRM",
                proposed_questions=[
                    clarification.ProposedQuestionIn(
                        topic=f"scope_{i}", question=f"scope question {i}?"
                    )
                    for i in range(6)
                ],
            ),
            submitted_by="agent.ceo",
        )
    )
    assert batch.assumptions, "the excess question should log an assumption"
    restated = run_async(
        lambda: _read(api, lambda conn: clarification.assumptions_for_release(conn, uuid.UUID(pid)))
    )
    assert restated, "a high-visibility assumption should surface for H12"

    # H2 (ceo_vision): induced G2 failure → retry → pass (the failure-path routing).
    bad = _submit(api, pid, "ceo_vision", _BAD_BRIEF)
    assert bad.outcome == "gate_failed_retry" and bad.attempt == 1
    good = _submit(api, pid, "ceo_vision", _BRIEF)
    assert good.outcome == "advanced" and good.next_stage == "pm_backlog"

    # H3–H12: every stage advances (H6 via the override; H9 ACWC opens debt).
    for stage, body in _STAGES_AFTER_BRIEF:
        outcome = _submit(api, pid, stage, body)
        assert outcome.outcome == "advanced", f"{stage}: {outcome.gate_reasons}"

    # The pipeline reached the terminal handover and is released.
    pipeline = api["client"].get(f"/api/v1/projects/{pid}/pipeline", headers=hdr).json()
    assert pipeline["pipeline"]["status"] == "released"
    assert pipeline["pipeline"]["current_stage"] == "handover"

    # The gate ledger records the induced failure + the passes (queryable).
    ledger = api["client"].get(f"/api/v1/projects/{pid}/handoffs/gate-ledger", headers=hdr).json()
    verdicts = [(e["stage"], e["verdict"]) for e in ledger["evaluations"]]
    assert ("ceo_vision", "fail") in verdicts and ("ceo_vision", "pass") in verdicts
    assert ("ceo_release", "pass") in verdicts

    # The ACWC review opened a debt ticket (WFL-021).
    debt_ledger = run_async(lambda: _read(api, lambda conn: debt.ledger(conn, uuid.UUID(pid))))
    assert any(t.source == "code_review" for t in debt_ledger.tickets)

    # A self-improvement loop converges on a real task.
    tid = _loop_task(api, pid)
    outcome = cast(
        loop.LoopOutcome,
        run_async(
            lambda: loop.run_loop(
                api["engine"],
                uuid.UUID(tid),
                _CleanDriver(),
                actor_user_id=api["user_ids"]["owner"],
                pr_diff="- old\n+ new",
                task_spec="Implement the widget",
                architecture_excerpt="Component: WidgetService (use_cases)",
            )
        ),
    )
    assert outcome.status == "converged" and outcome.cycles == 2 and outcome.task_status == "merged"
    loop_view = api["client"].get(f"/api/v1/tasks/{tid}/loop", headers=hdr).json()
    assert len(loop_view["cycles"]) == 2

    # Reflection + retrospective land in organization memory (Phase 40). The
    # reflection is produced by the SYSTEM's pipeline driven by a mock LLM gateway.
    run_async(
        lambda: memory.generate_task_reflection(
            api["engine"],
            _MockGateway(),
            project_id=uuid.UUID(pid),
            task_id=uuid.UUID(tid),
            agent_id="backend-bot",
            context="Implemented the widget and merged after a clean loop.",
            model_alias=None,
        )
    )
    assert run_async(lambda: memory.task_has_reflection(api["engine"], uuid.UUID(tid))) is True
    retro = run_async(
        lambda: memory.sprint_retrospective(
            api["engine"],
            project_id=uuid.UUID(pid),
            task_ids=[uuid.UUID(tid)],
            actor_agent="agent.pm",
        )
    )
    assert retro.reflections_aggregated >= 1

    # Milestone progression M0 → M4.
    flags: dict[str, Any] = {}
    for milestone, added in [
        ("M0", {"brief_accepted": True, "backlog_accepted": True}),
        ("M1", {"skeleton_deployed": True, "hello_path_green": True}),
        ("M2", {"must_stories_done": True, "g6_g7_passed": True}),
        ("M3", {"g8_g9_passed": True, "perf_budgets_met": True, "debt_reviewed": True}),
        ("M4", {"g10_g12_passed": True, "docs_live": True, "user_notified": True}),
    ]:
        flags.update(added)
        result = run_async(
            lambda: planning.sync_milestone(
                api["engine"],
                uuid.UUID(pid),
                planning.MilestoneSignalsIn(**flags),
                actor="agent.pm",
            )
        )
        assert result.current == milestone

    # The six derived artifacts reflect the whole run, exported via the API.
    regen = api["client"].post(f"/api/v1/projects/{pid}/artifacts:regenerate", headers=hdr)
    assert regen.status_code == 200 and len(regen.json()) == 6

    roadmap = api["client"].get(f"/api/v1/projects/{pid}/artifacts/roadmap", headers=hdr).json()
    assert roadmap["content_json"]["current_milestone"] == "M4"

    release = (
        api["client"].get(f"/api/v1/projects/{pid}/artifacts/release_plan", headers=hdr).json()
    )
    trace_features = {r["feature_id"] for r in release["content_json"]["traceability"]["rows"]}
    assert "F1" in trace_features

    debt_report = (
        api["client"].get(f"/api/v1/projects/{pid}/artifacts/debt_report", headers=hdr).json()
    )
    assert debt_report["content_json"]["open_total"] >= 1

    risk = api["client"].get(f"/api/v1/projects/{pid}/artifacts/risk_analysis", headers=hdr).json()
    assert any(r["category"] == "assumption" for r in risk["content_json"]["risks"])


async def _read(api: dict[str, Any], fn: Any) -> Any:
    async with api["engine"].connect() as conn:
        return await fn(conn)
