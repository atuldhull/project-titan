"""The execution-side acceptance gates G6–G12 (Phase 49; Vol 6 §2.3 H6–H12).

Pure unit tests — no Docker, no DB. Each gate gets a PASS fixture and a FAIL fixture,
asserting the verdict, that the reasons carry the §2.3 failure-path payload, and that
the evaluator identity (TITAN-WFL-004) is stamped."""

from typing import Any

from titan.handoffs.gates import GateContext
from titan.handoffs.standard_gates_release import (
    RELEASE_GATES,
    DeploymentRecordGate,
    DocBundleGate,
    MergeCandidateGate,
    QAReportGate,
    ReleaseApprovalGate,
    ReleaseReviewGate,
    SecurityClearanceGate,
    medium_security_findings,
)


def _ctx(stage: str, body: dict[str, Any], **kw: Any) -> GateContext:
    return GateContext(stage=stage, body=body, **kw)


def test_all_seven_release_stages_have_a_registered_gate() -> None:
    assert set(RELEASE_GATES) == {
        "engineering",
        "qa",
        "security",
        "code_review",
        "devops_deploy",
        "documentation",
        "ceo_release",
    }
    # Every gate stamps the WFL-004 evaluator identity.
    result = MergeCandidateGate().evaluate(_ctx("engineering", {}))
    assert result.evaluator_model == "rules" and result.prompt_version == "G6/v1"


# --- G6: CI + dual migration -------------------------------------------------

_CI_GREEN = {
    "lint": True,
    "typecheck": True,
    "unit": True,
    "integration": True,
    "coverage_ok": True,
}
_MIGRATION_OK = {"fresh_ok": True, "snapshot_ok": True}


def test_g6_passes_when_ci_green_and_migrations_apply_both_ways() -> None:
    ctx = _ctx("engineering", {}, checks={"ci": _CI_GREEN, "migration": _MIGRATION_OK})
    assert MergeCandidateGate().evaluate(ctx).passed


def test_g6_fails_closed_when_checks_absent() -> None:
    result = MergeCandidateGate().evaluate(_ctx("engineering", {}))
    assert not result.passed
    assert any("CI was not run" in r for r in result.reasons)
    assert any("Refactor" in r for r in result.reasons)  # the failure-path routing


def test_g6_fails_on_red_ci_or_bad_migration() -> None:
    red = _ctx(
        "engineering",
        {},
        checks={
            "ci": {**_CI_GREEN, "integration": False},
            "migration": {"fresh_ok": True, "snapshot_ok": False},
        },
    )
    result = MergeCandidateGate().evaluate(red)
    assert not result.passed
    assert any("integration" in r for r in result.reasons)
    assert any("snapshot" in r for r in result.reasons)


# --- G7: QA report -----------------------------------------------------------


def _qa(defects: list[dict[str, Any]], e2e_failed: int = 0, e2e_total: int = 5) -> dict[str, Any]:
    return {
        "defects": defects,
        "test_matrix": [
            {
                "suite": "e2e",
                "passed": e2e_total - e2e_failed,
                "failed": e2e_failed,
                "total": e2e_total,
            },
            {"suite": "regression", "passed": 10, "failed": 0, "total": 10},
        ],
    }


def test_g7_passes_with_no_blocking_defects_and_green_e2e() -> None:
    ctx = _ctx("qa", _qa(defects=[{"id": "D1", "severity": "P3", "owner_role": "backend"}]))
    assert QAReportGate().evaluate(ctx).passed


def test_g7_fails_on_p0_defect_and_routes_to_owner() -> None:
    ctx = _ctx("qa", _qa(defects=[{"id": "D9", "severity": "P0", "owner_role": "backend"}]))
    result = QAReportGate().evaluate(ctx)
    assert not result.passed
    assert any("D9" in r and "backend" in r for r in result.reasons)


def test_g7_fails_when_e2e_not_100_percent() -> None:
    result = QAReportGate().evaluate(_ctx("qa", _qa(defects=[], e2e_failed=1)))
    assert not result.passed
    assert any("e2e critical-path" in r for r in result.reasons)


# --- G8: security clearance --------------------------------------------------


def _sec(
    sast: list[dict[str, Any]], *, clean_secrets: bool = True, rbac: bool = True
) -> dict[str, Any]:
    return {
        "sast": sast,
        "dependency_audit": [],
        "secret_scan": {"clean": clean_secrets, "findings": []},
        "owasp": [{"category": "A01", "status": "pass", "evidence": "ok"}],
        "rbac_verified": rbac,
    }


def test_g8_passes_with_zero_high_critical_and_clean() -> None:
    ctx = _ctx(
        "security", _sec(sast=[{"id": "S1", "severity": "medium", "title": "x", "evidence": "e"}])
    )
    assert SecurityClearanceGate().evaluate(ctx).passed  # a medium does NOT block
    # ...but the medium IS surfaced as accepted-risk debt.
    assert medium_security_findings(ctx.body) == ["SAST S1: x"]


def test_g8_fails_on_high_finding() -> None:
    ctx = _ctx(
        "security", _sec(sast=[{"id": "S2", "severity": "high", "title": "rce", "evidence": "e"}])
    )
    result = SecurityClearanceGate().evaluate(ctx)
    assert not result.passed and any("S2" in r for r in result.reasons)


def test_g8_fails_on_dirty_secret_scan_or_unverified_rbac() -> None:
    dirty = SecurityClearanceGate().evaluate(_ctx("security", _sec(sast=[], clean_secrets=False)))
    assert not dirty.passed and any("secret scan" in r for r in dirty.reasons)
    norbac = SecurityClearanceGate().evaluate(_ctx("security", _sec(sast=[], rbac=False)))
    assert not norbac.passed and any("RBAC" in r for r in norbac.reasons)


# --- G9: release review ------------------------------------------------------


def test_g9_passes_on_approved_and_acwc() -> None:
    for verdict in ("APPROVED", "APPROVED_WITH_COMMENTS"):
        result = ReleaseReviewGate().evaluate(
            _ctx("code_review", {"verdict": verdict, "comments": ["c"]})
        )
        assert result.passed, verdict


def test_g9_fails_on_rejected_with_rework_tickets() -> None:
    ctx = _ctx(
        "code_review", {"verdict": "REJECTED", "rework_tickets": ["fix boundary in billing"]}
    )
    result = ReleaseReviewGate().evaluate(ctx)
    assert not result.passed
    assert any("REJECTED" in r for r in result.reasons)
    assert any("fix boundary" in r for r in result.reasons)


# --- G10: deployment record --------------------------------------------------


def test_g10_passes_when_smoke_and_health_green() -> None:
    ctx = _ctx(
        "devops_deploy", {"smoke_tests": {"passed": 5, "failed": 0}, "health_checks": "green"}
    )
    assert DeploymentRecordGate().evaluate(ctx).passed


def test_g10_fails_on_red_health_and_names_rollback() -> None:
    ctx = _ctx("devops_deploy", {"smoke_tests": {"passed": 4, "failed": 1}, "health_checks": "red"})
    result = DeploymentRecordGate().evaluate(ctx)
    assert not result.passed
    assert any("smoke" in r for r in result.reasons)
    assert any("health checks" in r for r in result.reasons)
    assert any("roll back" in r for r in result.reasons)  # the failure-path routing


# --- G11: doc bundle ---------------------------------------------------------


def test_g11_passes_with_verified_quickstart_and_ok_coverage() -> None:
    ctx = _ctx(
        "documentation",
        {"quickstart_verified": True, "documented_endpoints": ["GET /x"], "adr_index": ["ADR-1"]},
        checks={"doc": {"ok": True}},
    )
    assert DocBundleGate().evaluate(ctx).passed


def test_g11_fails_when_quickstart_did_not_execute_and_routes_to_h7() -> None:
    ctx = _ctx(
        "documentation",
        {"quickstart_verified": False, "documented_endpoints": ["x"], "adr_index": ["a"]},
    )
    result = DocBundleGate().evaluate(ctx)
    assert not result.passed
    assert any("H7" in r for r in result.reasons)  # a failing quickstart is a P1 → H7


def test_g11_fails_on_missing_endpoint_coverage() -> None:
    ctx = _ctx(
        "documentation",
        {"quickstart_verified": True, "documented_endpoints": [], "adr_index": []},
        checks={
            "doc": {"ok": False, "missing_endpoints": ["POST /widgets"], "missing_adrs": ["ADR-2"]}
        },
    )
    result = DocBundleGate().evaluate(ctx)
    assert not result.passed
    assert any("POST /widgets" in r for r in result.reasons)
    assert any("ADR-2" in r for r in result.reasons)


# --- G12: release approval ---------------------------------------------------


def test_g12_passes_when_approved_criteria_met_and_traced() -> None:
    ctx = _ctx(
        "ceo_release",
        {
            "approved": True,
            "success_criteria_met": [{"criterion": "login works", "met": True}],
            "traceability": [{"feature_id": "F1", "test_ids": ["T1"]}],
        },
    )
    assert ReleaseApprovalGate().evaluate(ctx).passed


def test_g12_fails_on_unmet_criterion_and_untraced_feature() -> None:
    ctx = _ctx(
        "ceo_release",
        {
            "approved": True,
            "success_criteria_met": [{"criterion": "billing works", "met": False}],
            "traceability": [{"feature_id": "F2", "test_ids": []}],
        },
    )
    result = ReleaseApprovalGate().evaluate(ctx)
    assert not result.passed
    assert any("billing works" in r and "backlog" in r for r in result.reasons)
    assert any("F2" in r and "acceptance test" in r for r in result.reasons)
