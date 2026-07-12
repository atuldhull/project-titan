"""Phase 43 — the concrete gates G1–G5 (Vol 6 §2.3 H1–H5).

Every gate is a pure predicate over a GateContext, so each gets a deterministic
pass fixture AND a fail fixture, and the fail asserts the §2.3 failure-path payload
(gap annotations, INVEST violations, the offending subgraph, numbered objections).
No database — the executor assembles the context; the gate is side-effect-free.
"""

from typing import Any

import pytest

from titan.handoffs.gates import GateContext
from titan.handoffs.standard_gates import (
    ArchitecturePackageGate,
    ProductBacklogGate,
    ProjectBriefGate,
    SprintPlanGate,
    UserRequestGate,
    evaluate_intake_gate,
)

pytestmark = pytest.mark.schema


def _ctx(stage: str, body: dict[str, Any], **kw: Any) -> GateContext:
    return GateContext(stage=stage, body=body, **kw)


# --- G1: UserRequest --------------------------------------------------------


def test_g1_passes_a_novel_nonempty_request() -> None:
    r = UserRequestGate().evaluate(
        _ctx("user_request", {"raw_text": "Build a CRM"}, active_project_texts=("Build a shop",))
    )
    assert r.passed and r.reasons == []
    assert r.evaluator_agent == "ceo" and r.prompt_version == "G1/v1"


def test_g1_fails_empty_request() -> None:
    r = UserRequestGate().evaluate(_ctx("user_request", {"raw_text": "   "}))
    assert not r.passed and any("empty" in x for x in r.reasons)


def test_g1_fails_duplicate_of_active_project() -> None:
    r = UserRequestGate().evaluate(
        _ctx(
            "user_request",
            {"raw_text": "Build a CRM"},
            active_project_texts=("build   a CRM",),  # normalization-insensitive match
        )
    )
    assert not r.passed and any("duplicate" in x for x in r.reasons)


def test_g1_intake_helper_matches_gate() -> None:
    assert evaluate_intake_gate("Build a CRM", ["Build a CRM"]).passed is False
    assert evaluate_intake_gate("Build a CRM", ["Something else"]).passed is True


# --- G2: ProjectBrief -------------------------------------------------------


def _brief(**over: Any) -> dict[str, Any]:
    base = {
        "features": [
            {"id": "F1", "name": "Login", "moscow": "must", "success_criterion": "auth < 1s"},
            {"id": "F2", "name": "Nice-to-have", "moscow": "could", "success_criterion": None},
        ],
        "non_goals": ["no mobile app"],
        "constraints": {"canonical_stack": True},
        "assumptions": [],
    }
    base.update(over)
    return base


def test_g2_passes_complete_brief() -> None:
    r = ProjectBriefGate().evaluate(_ctx("ceo_vision", _brief()))
    assert r.passed and r.evaluator_agent == "pm"


def test_g2_fails_must_feature_without_success_criterion() -> None:
    brief = _brief(
        features=[{"id": "F1", "name": "Login", "moscow": "must", "success_criterion": "  "}]
    )
    r = ProjectBriefGate().evaluate(_ctx("ceo_vision", brief))
    assert not r.passed
    assert any("F1" in x and "success criterion" in x for x in r.reasons)


def test_g2_fails_empty_non_goals() -> None:
    r = ProjectBriefGate().evaluate(_ctx("ceo_vision", _brief(non_goals=[])))
    assert not r.passed and any("non-goals" in x for x in r.reasons)


def test_g2_passes_stack_override() -> None:
    # §2.3 clause 3 is a waiver: canonical_stack=False IS the user's recorded
    # override, so G2 must not fail it (regardless of the assumptions section).
    brief = _brief(constraints={"canonical_stack": False}, assumptions=[])
    assert ProjectBriefGate().evaluate(_ctx("ceo_vision", brief)).passed


def test_g2_reason_names_feature_by_id_only() -> None:
    # TITAN-OPS-016: the gap annotation must not embed the free-text feature name.
    brief = _brief(
        features=[
            {
                "id": "F1",
                "name": "Secret internal codename Zeta",
                "moscow": "must",
                "success_criterion": None,
            }
        ]
    )
    r = ProjectBriefGate().evaluate(_ctx("ceo_vision", brief))
    assert not r.passed
    assert all("Secret internal codename Zeta" not in x for x in r.reasons)
    assert any("F1" in x for x in r.reasons)


# --- G3: ProductBacklog -----------------------------------------------------


def _story(**over: Any) -> dict[str, Any]:
    base = {
        "id": "S1",
        "as_a": "user",
        "i_want": "to log in",
        "so_that": "I can access my data",
        "acceptance_criteria": [{"given": "an account", "when": "I log in", "then": "I see home"}],
        "points": 3,
        "covers_features": ["F1"],
    }
    base.update(over)
    return base


def _brief_prior() -> dict[str, dict[str, Any]]:
    return {
        "ceo_vision": {
            "features": [
                {"id": "F1", "name": "Login", "moscow": "must"},
                {"id": "F2", "name": "Reports", "moscow": "should"},
            ]
        }
    }


def test_g3_passes_invest_stories_covering_musts() -> None:
    ctx = _ctx("pm_backlog", {"stories": [_story()]}, prior_artifacts=_brief_prior())
    r = ProductBacklogGate().evaluate(ctx)
    assert r.passed and r.evaluator_agent == "project_manager"


def test_g3_fails_uncovered_must_feature() -> None:
    prior = {"ceo_vision": {"features": [{"id": "F1", "name": "Login", "moscow": "must"}]}}
    ctx = _ctx("pm_backlog", {"stories": [_story(covers_features=[])]}, prior_artifacts=prior)
    r = ProductBacklogGate().evaluate(ctx)
    assert not r.passed and any("F1" in x and "not covered" in x for x in r.reasons)


def test_g3_fails_untestable_ac() -> None:
    bad = _story(acceptance_criteria=[{"given": "x", "when": "x", "then": "x"}])
    ctx = _ctx("pm_backlog", {"stories": [bad]}, prior_artifacts=_brief_prior())
    r = ProductBacklogGate().evaluate(ctx)
    assert not r.passed and any("not testable" in x for x in r.reasons)


def test_g3_fails_dangling_feature_reference() -> None:
    ctx = _ctx(
        "pm_backlog",
        {"stories": [_story(covers_features=["F1", "F99"])]},
        prior_artifacts=_brief_prior(),
    )
    r = ProductBacklogGate().evaluate(ctx)
    assert not r.passed and any("F99" in x and "unknown feature" in x for x in r.reasons)


def test_g3_partial_acceptance_lists_passing_stories() -> None:
    good = _story(id="S1")
    bad = _story(id="S2", as_a="  ", covers_features=[])  # missing actor
    ctx = _ctx("pm_backlog", {"stories": [good, bad]}, prior_artifacts=_brief_prior())
    r = ProductBacklogGate().evaluate(ctx)
    assert not r.passed
    assert any("S2" in x and "no actor" in x for x in r.reasons)
    assert any("accepted" in x and "S1" in x for x in r.reasons)  # partial acceptance


# --- G4: SprintPlan ---------------------------------------------------------


def _plan(**over: Any) -> dict[str, Any]:
    base = {
        "sprints": [{"number": 1, "goal": "skeleton", "walking_skeleton": True}],
        "tasks": [
            {"id": "T1", "estimate_days": 0.5, "sprint_number": 1, "story_id": "S1"},
            {"id": "T2", "estimate_days": 0.5, "sprint_number": 1, "story_id": "S1"},
        ],
        "dependencies": [{"task_id": "T2", "depends_on_id": "T1"}],
    }
    base.update(over)
    return base


def _backlog_prior() -> dict[str, dict[str, Any]]:
    return {"pm_backlog": {"stories": [{"id": "S1"}]}}


def test_g4_passes_acyclic_covered_plan() -> None:
    ctx = _ctx("pmgr_plan", _plan(), prior_artifacts=_backlog_prior())
    r = SprintPlanGate().evaluate(ctx)
    assert r.passed and r.evaluator_agent == "architect"


def test_g4_fails_dependency_cycle_reports_subgraph() -> None:
    plan = _plan(
        dependencies=[
            {"task_id": "T1", "depends_on_id": "T2"},
            {"task_id": "T2", "depends_on_id": "T1"},
        ]
    )
    r = SprintPlanGate().evaluate(_ctx("pmgr_plan", plan, prior_artifacts=_backlog_prior()))
    assert not r.passed
    cycle = next(x for x in r.reasons if "cycle" in x)
    assert "T1" in cycle and "T2" in cycle  # the offending subgraph


def test_g4_fails_story_without_task() -> None:
    prior = {"pm_backlog": {"stories": [{"id": "S1"}, {"id": "S2"}]}}
    r = SprintPlanGate().evaluate(_ctx("pmgr_plan", _plan(), prior_artifacts=prior))
    assert not r.passed and any("S2" in x and "maps to no task" in x for x in r.reasons)


def test_g4_fails_missing_walking_skeleton() -> None:
    plan = _plan(sprints=[{"number": 1, "goal": "x", "walking_skeleton": False}])
    r = SprintPlanGate().evaluate(_ctx("pmgr_plan", plan, prior_artifacts=_backlog_prior()))
    assert not r.passed and any("walking-skeleton" in x for x in r.reasons)


def test_g4_fails_over_capacity_reports_tasks() -> None:
    plan = _plan(
        tasks=[
            {"id": "T1", "estimate_days": 1.0, "sprint_number": 1, "story_id": "S1"},
            {"id": "T2", "estimate_days": 1.0, "sprint_number": 1, "story_id": "S1"},
        ]
    )
    ctx = _ctx("pmgr_plan", plan, prior_artifacts=_backlog_prior(), sprint_capacity_days=1.5)
    r = SprintPlanGate().evaluate(ctx)
    assert not r.passed
    over = next(x for x in r.reasons if "over capacity" in x)
    assert "T1" in over and "T2" in over


def test_g4_fails_dangling_dependency_edge() -> None:
    plan = _plan(dependencies=[{"task_id": "T1", "depends_on_id": "T404"}])
    r = SprintPlanGate().evaluate(_ctx("pmgr_plan", plan, prior_artifacts=_backlog_prior()))
    assert not r.passed and any("T404" in x for x in r.reasons)


# --- G5: ArchitecturePackage ------------------------------------------------


def _pkg(**over: Any) -> dict[str, Any]:
    base = {
        "components": [
            {"name": "OrderEntity", "layer": "entities", "depends_on": []},
            {"name": "CreateOrder", "layer": "use_cases", "depends_on": ["OrderEntity"]},
            {"name": "OrderRouter", "layer": "interface_adapters", "depends_on": ["CreateOrder"]},
        ],
        "api_contracts": [
            {
                "path": "/orders",
                "method": "POST",
                "operation": {"responses": {"201": {"description": "created"}}},
            }
        ],
        "task_components": [{"task_id": "T1", "component": "CreateOrder"}],
    }
    base.update(over)
    return base


def _plan_prior() -> dict[str, dict[str, Any]]:
    return {"pmgr_plan": {"tasks": [{"id": "T1"}]}}


def _g5_ctx(pkg: dict[str, Any], version: int | None = 1) -> GateContext:
    return _ctx(
        "architect_design",
        pkg,
        prior_artifacts=_plan_prior(),
        constitution_version=version,
        constitution_body="## 6. constitution",
    )


def test_g5_passes_compliant_package() -> None:
    r = ArchitecturePackageGate().evaluate(_g5_ctx(_pkg()))
    assert r.passed, r.reasons
    assert r.evaluator_agent == "code_reviewer"
    assert r.constitution_version == 1  # stamped for the ledger


def test_g5_fails_outward_dependency() -> None:
    pkg = _pkg(
        components=[
            {"name": "OrderEntity", "layer": "entities", "depends_on": ["OrderRouter"]},
            {"name": "OrderRouter", "layer": "interface_adapters", "depends_on": []},
        ],
        task_components=[{"task_id": "T1", "component": "OrderRouter"}],
    )
    r = ArchitecturePackageGate().evaluate(_g5_ctx(pkg))
    assert not r.passed
    assert any("inward-only" in x and x.startswith("1.") for x in r.reasons)  # numbered objection


def test_g5_fails_unmapped_task() -> None:
    r = ArchitecturePackageGate().evaluate(_g5_ctx(_pkg(task_components=[])))
    assert not r.passed and any("T1" in x and "not mapped" in x for x in r.reasons)


def test_g5_fails_invalid_openapi_operation() -> None:
    pkg = _pkg(api_contracts=[{"path": "/o", "method": "GET", "operation": {}}])
    r = ArchitecturePackageGate().evaluate(_g5_ctx(pkg))
    assert not r.passed and any("OpenAPI 3.1" in x for x in r.reasons)


def test_g5_fails_response_without_description() -> None:
    pkg = _pkg(
        api_contracts=[{"path": "/o", "method": "GET", "operation": {"responses": {"200": {}}}}]
    )
    r = ArchitecturePackageGate().evaluate(_g5_ctx(pkg))
    assert not r.passed and any("OpenAPI 3.1" in x for x in r.reasons)


def test_g5_fails_cqrs_without_adr() -> None:
    pkg = _pkg(
        components=[
            {"name": "CreateOrderCommand", "layer": "use_cases", "depends_on": []},
            {"name": "OrderQuery", "layer": "use_cases", "depends_on": []},
        ],
        task_components=[{"task_id": "T1", "component": "OrderQuery"}],
    )
    r = ArchitecturePackageGate().evaluate(_g5_ctx(pkg))
    assert not r.passed and any("CQRS" in x for x in r.reasons)


def test_g5_does_not_flag_non_cqrs_command_query_names() -> None:
    # Regression: "CommandBus"/"QueryBuilder" are not a CQRS split (Command/Query
    # is a prefix, not the …Command/…Query suffix convention) — must not be flagged.
    pkg = _pkg(
        components=[
            {"name": "CommandBus", "layer": "interface_adapters", "depends_on": []},
            {"name": "QueryBuilder", "layer": "interface_adapters", "depends_on": []},
        ],
        task_components=[{"task_id": "T1", "component": "CommandBus"}],
    )
    assert ArchitecturePackageGate().evaluate(_g5_ctx(pkg)).passed


def test_g5_passes_cqrs_with_adr() -> None:
    pkg = _pkg(
        components=[
            {"name": "CreateOrderCommand", "layer": "use_cases", "depends_on": []},
            {"name": "OrderQuery", "layer": "use_cases", "depends_on": []},
        ],
        adrs=[{"title": "Adopt CQRS", "decision": "split", "rationale": "10x read/write skew"}],
        task_components=[
            {"task_id": "T1", "component": "OrderQuery"},
        ],
    )
    assert ArchitecturePackageGate().evaluate(_g5_ctx(pkg)).passed


def test_g5_skips_task_coverage_without_plan() -> None:
    # No prior SprintPlan → task-coverage is not checkable, so it is skipped.
    ctx = _ctx(
        "architect_design",
        _pkg(task_components=[]),
        constitution_version=1,
        constitution_body="c",
    )
    assert ArchitecturePackageGate().evaluate(ctx).passed
