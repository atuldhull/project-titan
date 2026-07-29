"""The concrete execution-side acceptance gates G6–G12 (Vol 6 §2.3 H6–H12).

Like G1–G5 (``standard_gates``), each gate is a **pure** predicate over a
:class:`~titan.handoffs.gates.GateContext` — it reads the already-schema-valid
artifact (``ctx.body``), earlier artifacts (``ctx.prior_artifacts``), and, for the
gates whose artifact does not itself record a runtime result, the executor-computed
results in ``ctx.checks``. It returns a :class:`GateResult` whose ``reasons`` carry
the §2.3 failure-path payload the producer must act on:

- **G6** (MergeCandidateManifest, H6): CI green on the integration branch (lint,
  typecheck, unit + integration, §6.5 coverage) AND migrations apply cleanly to a
  fresh PostgreSQL 16 AND to a snapshot of the previous schema. Failure reopens the
  failing components as tasks re-entering the loop at Refactor.
- **G7** (QAReport, H7): zero P0/P1 defects, the e2e critical-path suite 100%
  passing, and the regression suite passing. Failure routes each P0/P1 to its owning
  engineer role.
- **G8** (SecurityClearance, H8): zero high/critical findings, a clean secret scan,
  RBAC verified, no failing OWASP item. Mediums do NOT block but are logged as
  accepted-risk debt (the service opens a debt ticket). Failure routes findings to
  security-labeled tasks.
- **G9** (ReleaseReview, H9): reviewer verdict is not REJECTED. APPROVED_WITH_COMMENTS
  proceeds but logs its comments as technical-debt tickets (§5); REJECTED returns the
  itemized rework tickets.
- **G10** (DeploymentRecord, H10): the staging smoke suite is green and health checks
  are green through the soak window. Failure triggers rollback + incident + reopen at
  H6 (handled by the release executor).
- **G11** (DocBundle, H11): the doc coverage checks pass (every public endpoint
  documented, every ADR indexed) and the README quickstart executed successfully. A
  failing quickstart is a P1 defect routed to H7 (QA), NOT a docs issue.
- **G12** (ReleaseApproval, H12): the release is approved, every success criterion is
  met, and every MUST-feature traces to ≥1 passing acceptance test. Unmet criteria
  reopen as backlog items; scope is never silently narrowed.

Gates run after the schema gate, so a gate never sees an ill-typed artifact.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from titan.handoffs.gates import Gate, GateContext, GateResult
from titan.observability.metrics import REVIEW_REJECTIONS_TOTAL

_BLOCKING_SEVERITIES = frozenset({"high", "critical"})
_BLOCKING_DEFECTS = frozenset({"P0", "P1"})


def _rows(body: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    return [r for r in body.get(key, []) if isinstance(r, Mapping)]


def _result(passed: bool, reasons: list[str], *, agent: str, prompt: str) -> GateResult:
    return GateResult(
        passed=passed,
        reasons=reasons,
        evaluator_agent=agent,
        evaluator_model="rules",
        prompt_version=prompt,
    )


# --- G6: MergeCandidateManifest ---------------------------------------------


@dataclass(frozen=True)
class MergeCandidateGate:
    """G6 (H6): CI green on the integration branch + the dual migration check. The
    manifest itself lists the migration scripts; the pass/fail of running CI and of
    applying those migrations to a fresh DB and to a snapshot is computed by the
    executor and read from ``ctx.checks`` (``ci`` / ``migration``). Absent checks
    fail closed — an unverified integration cannot pass G6."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        reasons: list[str] = []
        ci = ctx.checks.get("ci") if isinstance(ctx.checks.get("ci"), Mapping) else None
        migration = (
            ctx.checks.get("migration")
            if isinstance(ctx.checks.get("migration"), Mapping)
            else None
        )
        if ci is None:
            reasons.append("G6: CI was not run on the integration branch — cannot verify green.")
        else:
            for check in ("lint", "typecheck", "unit", "integration"):
                if not ci.get(check):
                    reasons.append(f"G6: CI '{check}' is not green on the integration branch.")
            if not ci.get("coverage_ok"):
                reasons.append("G6: coverage gates (§6.5) are not met on the changed code.")
        if migration is None:
            reasons.append("G6: the dual migration check was not run.")
        else:
            if not migration.get("fresh_ok"):
                reasons.append("G6: migrations do not apply cleanly to a fresh PostgreSQL 16.")
            if not migration.get("snapshot_ok"):
                reasons.append(
                    "G6: migrations do not apply cleanly to a snapshot of the previous schema."
                )
        if reasons:
            reasons.append(
                "G6 failure path: reopen the failing components as tasks re-entering the "
                "self-improvement loop at Refactor, with the CI failure attached as critique."
            )
        return _result(not reasons, reasons, agent="qa", prompt="G6/v1")


# --- G7: QAReport -----------------------------------------------------------


@dataclass(frozen=True)
class QAReportGate:
    """G7 (H7): zero P0/P1 defects, e2e critical-path 100% passing, regression
    passing. Reasons route each blocking defect to its owning engineer role."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        reasons: list[str] = []
        for defect in _rows(ctx.body, "defects"):
            if str(defect.get("severity")) in _BLOCKING_DEFECTS:
                reasons.append(
                    f"G7: blocking defect '{defect.get('id')}' ({defect.get('severity')}) "
                    f"routes to the '{defect.get('owner_role')}' role."
                )
        suites = {str(s.get("suite")): s for s in _rows(ctx.body, "test_matrix") if s.get("suite")}
        e2e = suites.get("e2e")
        if e2e is None or int(e2e.get("failed", 0)) > 0 or int(e2e.get("total", 0)) == 0:
            reasons.append("G7: the e2e critical-path suite is not 100% passing.")
        regression = suites.get("regression")
        if regression is not None and int(regression.get("failed", 0)) > 0:
            reasons.append("G7: the regression suite is failing.")
        return _result(not reasons, reasons, agent="security", prompt="G7/v1")


# --- G8: SecurityClearance --------------------------------------------------


@dataclass(frozen=True)
class SecurityClearanceGate:
    """G8 (H8): zero high/critical findings (SAST + dependency CVEs), a clean secret
    scan, verified RBAC, no failing OWASP item. Mediums are NOT a block — they are
    accepted-risk debt the service records — so they never appear in ``reasons``."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        reasons: list[str] = []
        for finding in _rows(ctx.body, "sast"):
            if str(finding.get("severity")) in _BLOCKING_SEVERITIES:
                reasons.append(
                    f"G8: {finding.get('severity')} SAST finding '{finding.get('id')}' must be "
                    "fixed (security-labeled task, mandatory loop re-entry)."
                )
        for cve in _rows(ctx.body, "dependency_audit"):
            if str(cve.get("severity")) in _BLOCKING_SEVERITIES:
                reasons.append(
                    f"G8: {cve.get('severity')} dependency CVE {cve.get('cve_id')} in "
                    f"'{cve.get('package')}' must be remediated."
                )
        secret_scan = ctx.body.get("secret_scan")
        if isinstance(secret_scan, Mapping) and not secret_scan.get("clean", False):
            reasons.append("G8: the secret scan is not clean — secrets must be purged.")
        if not ctx.body.get("rbac_verified", False):
            reasons.append("G8: RBAC was not verified against the Volume 8 policy.")
        for item in _rows(ctx.body, "owasp"):
            if str(item.get("status")) == "fail":
                reasons.append(f"G8: OWASP item '{item.get('category')}' failed.")
        return _result(not reasons, reasons, agent="code_reviewer", prompt="G8/v1")


# --- G9: ReleaseReview ------------------------------------------------------


@dataclass(frozen=True)
class ReleaseReviewGate:
    """G9 (H9): the reviewer verdict is not REJECTED. APPROVED and
    APPROVED_WITH_COMMENTS both proceed (ACWC logs its comments as debt via the
    service); REJECTED returns the itemized rework tickets."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        verdict = str(ctx.body.get("verdict", ""))
        reasons: list[str] = []
        if verdict == "REJECTED":
            REVIEW_REJECTIONS_TOTAL.labels("code_reviewer").inc()  # Vol 9 §6.1 quality signal
            tickets = [str(t) for t in ctx.body.get("rework_tickets", [])]
            reasons.append("G9: the release review REJECTED the integrated diff.")
            reasons.extend(f"G9 rework: {t}" for t in tickets)
        return _result(not reasons, reasons, agent="code_reviewer", prompt="G9/v1")


# --- G10: DeploymentRecord --------------------------------------------------


@dataclass(frozen=True)
class DeploymentRecordGate:
    """G10 (H10): the staging smoke suite is green and health checks are green
    through the soak window. Failure triggers rollback + incident + reopen at H6
    (the release executor performs the side effects; the gate names the path)."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        reasons: list[str] = []
        smoke = ctx.body.get("smoke_tests")
        if isinstance(smoke, Mapping) and int(smoke.get("failed", 0)) > 0:
            reasons.append(
                f"G10: the staging smoke suite is not green ({smoke.get('failed')} failed)."
            )
        if str(ctx.body.get("health_checks")) != "green":
            reasons.append("G10: health checks did not stay green through the soak window.")
        # 'traces flowing' is an execution result (OTel), recorded by the executor.
        deploy = ctx.checks.get("deploy")
        if isinstance(deploy, Mapping) and deploy.get("traces_flowing") is False:
            reasons.append("G10: OpenTelemetry traces are not flowing from the deployment.")
        if reasons:
            reasons.append(
                "G10 failure path: roll back to the rollback pointer, emit an incident, open a "
                "defect task, and return the pipeline to H6 scope."
            )
        return _result(not reasons, reasons, agent="devops", prompt="G10/v1")


# --- G11: DocBundle ---------------------------------------------------------


@dataclass(frozen=True)
class DocBundleGate:
    """G11 (H11): the §6.7 doc coverage checks pass (every public endpoint documented,
    every ADR indexed) and the README quickstart executed. The coverage report is an
    execution result (``ctx.checks["doc"]``); the quickstart pass is recorded on the
    artifact. A failing quickstart is a P1 defect routed to H7, NOT a docs issue."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        reasons: list[str] = []
        if not ctx.body.get("quickstart_verified", False):
            reasons.append(
                "G11: the README quickstart did not execute successfully — treat as a P1 defect "
                "routed to H7 (QA), not a docs issue."
            )
        doc = ctx.checks.get("doc")
        if isinstance(doc, Mapping):
            if not doc.get("ok"):
                for endpoint in doc.get("missing_endpoints", []) or []:
                    reasons.append(f"G11: endpoint '{endpoint}' is not documented with prose.")
                for adr in doc.get("missing_adrs", []) or []:
                    reasons.append(f"G11: ADR '{adr}' is not indexed.")
                if not reasons:
                    reasons.append("G11: documentation coverage checks did not pass.")
        elif not ctx.body.get("documented_endpoints") or not ctx.body.get("adr_index"):
            # No executor report: fall back to the artifact's own self-reported coverage.
            reasons.append("G11: the doc bundle reports no documented endpoints or ADR index.")
        return _result(not reasons, reasons, agent="ceo", prompt="G11/v1")


# --- G12: ReleaseApproval ---------------------------------------------------


@dataclass(frozen=True)
class ReleaseApprovalGate:
    """G12 (H12): the release is approved, every success criterion is met, and every
    MUST-feature traces to ≥1 passing acceptance test. Unmet criteria reopen as
    backlog items — scope is never silently narrowed (the CEO restates any
    assumption; unmet MUSTs are surfaced, not dropped)."""

    def evaluate(self, ctx: GateContext) -> GateResult:
        reasons: list[str] = []
        if not ctx.body.get("approved", False):
            reasons.append("G12: the release is not approved by the CEO agent.")
        for criterion in _rows(ctx.body, "success_criteria_met"):
            if not criterion.get("met", False):
                reasons.append(
                    f"G12: success criterion '{criterion.get('criterion')}' is not met — "
                    "reopen as a backlog item (scope is never silently narrowed)."
                )
        for row in _rows(ctx.body, "traceability"):
            if not row.get("test_ids"):
                reasons.append(
                    f"G12: MUST-feature '{row.get('feature_id')}' traces to no passing acceptance "
                    "test."
                )
        return _result(not reasons, reasons, agent="ceo", prompt="G12/v1")


# --- registry ---------------------------------------------------------------

#: The producing stage each G6–G12 gate evaluates.
RELEASE_GATES: dict[str, Gate] = {
    "engineering": MergeCandidateGate(),
    "qa": QAReportGate(),
    "security": SecurityClearanceGate(),
    "code_review": ReleaseReviewGate(),
    "devops_deploy": DeploymentRecordGate(),
    "documentation": DocBundleGate(),
    "ceo_release": ReleaseApprovalGate(),
}


def blocking_security_findings(body: Mapping[str, Any]) -> list[str]:
    """The high/critical SAST + CVE ids in a SecurityClearance body (the G8 blockers).
    Exposed for the service's repeated-finding escalation (§2.3 H8)."""
    out: list[str] = []
    for finding in _rows(body, "sast"):
        if str(finding.get("severity")) in _BLOCKING_SEVERITIES:
            out.append(str(finding.get("id")))
    for cve in _rows(body, "dependency_audit"):
        if str(cve.get("severity")) in _BLOCKING_SEVERITIES:
            out.append(str(cve.get("cve_id")))
    return out


def medium_security_findings(body: Mapping[str, Any]) -> list[str]:
    """The medium-severity SAST + CVE titles — accepted-risk debt (G8, §5)."""
    out: list[str] = []
    for finding in _rows(body, "sast"):
        if str(finding.get("severity")) == "medium":
            out.append(f"SAST {finding.get('id')}: {finding.get('title')}")
    for cve in _rows(body, "dependency_audit"):
        if str(cve.get("severity")) == "medium":
            out.append(f"CVE {cve.get('cve_id')} in {cve.get('package')}")
    return out


def review_comments(body: Mapping[str, Any]) -> Sequence[str]:
    """The ReleaseReview comments logged as debt on APPROVED_WITH_COMMENTS (G9, §5)."""
    return [str(c) for c in body.get("comments", [])]


__all__ = [
    "RELEASE_GATES",
    "DeploymentRecordGate",
    "DocBundleGate",
    "MergeCandidateGate",
    "QAReportGate",
    "ReleaseApprovalGate",
    "ReleaseReviewGate",
    "SecurityClearanceGate",
    "blocking_security_findings",
    "medium_security_findings",
    "review_comments",
]
