"""The Section-7 KPI report (Vol 1 §7, TITAN-FND-007/052, Phase 55).

One :class:`BriefKpis` per brief run, aggregated into :class:`SuiteKpis` (the §7
table columns) and checked against the MVP threshold column. Metrics trace to real
signals: completion to the pipeline reaching *released*; first-draft rejection to a
reopened stage in the gate ledger; cost to the gateway ``llm_usage`` ledger; crash
recovery to the chaos suite; human interventions to clarifications + approvals
(displayed, never driven to zero — approval gates are a feature).

An MVP threshold is only marked PASS/FAIL when the underlying metric was actually
MEASURED this run: coverage is fixture-*declared* under the mock profile (the real
measured coverage comes from the Phase-56 offline run), cost is measured only if a
metered gateway call landed, and crash recovery is a SIMULATED (in-process) sweep.
Un-measured metrics are ``met=None`` (displayed, not a green pass) so the table
never shows a checkmark for something it did not measure.
"""

import statistics
from typing import Literal

from pydantic import BaseModel, Field

CoverageSource = Literal["declared", "measured"]

# §7 MVP threshold column.
MVP_MIN_COMPLETION = 0.50
MVP_MIN_FIRST_DRAFT_REJECTION = 0.30
MVP_MAX_TTFD_SECONDS = 4 * 60 * 60
MVP_MIN_LOCAL_VIABLE = 3
MVP_MAX_COST_USD = 15.0
MVP_MIN_COVERAGE = 0.60
MVP_CRASH_RECOVERY = 1.0


class BriefKpis(BaseModel):
    """The per-brief KPI record."""

    brief_id: str
    executed: bool = False
    completed: bool = False
    first_draft_rejected: bool = False
    time_to_first_deploy_seconds: float | None = None
    local_only: bool = False
    cost_usd: float = 0.0
    cost_measured: bool = False
    generated_test_coverage: float = 0.0
    coverage_source: CoverageSource = "declared"
    human_intervention_count: int = 0


class SuiteKpis(BaseModel):
    """The §7 aggregate columns for one run."""

    briefs_total: int
    briefs_executed: int
    autonomous_completion_rate: float = Field(ge=0.0, le=1.0)
    first_draft_rejection_rate: float = Field(ge=0.0, le=1.0)
    time_to_first_deploy_seconds: float | None = None
    local_only_count: int = 0
    local_only_viability: str = "0/0"
    cost_per_project_usd: float | None = None
    cost_measured: bool = False
    generated_test_coverage: float | None = None
    coverage_source: CoverageSource | None = None
    crash_recovery_rate: float | None = None
    human_intervention_count: int = 0


class MvpThreshold(BaseModel):
    """One §7 KPI checked against its MVP target. ``met`` is ``None`` when the
    metric is displayed-only (human intervention) or was not measured this run."""

    kpi: str
    target: str
    value: str
    met: bool | None = None


class KpiReport(BaseModel):
    """A complete Section-7 KPI report for one benchmark run."""

    run_id: str
    profile: str = "mock"
    suite_version: str = "1"
    per_brief: list[BriefKpis] = Field(default_factory=list)
    suite: SuiteKpis
    mvp: list[MvpThreshold] = Field(default_factory=list)
    generated_at: str | None = None

    @property
    def meets_mvp(self) -> bool:
        """True iff no measured MVP threshold is failed (displayed-only metrics
        never fail the run)."""
        return all(t.met is not False for t in self.mvp)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def build_suite_kpis(
    per_brief: list[BriefKpis], *, briefs_total: int, crash_recovery_rate: float | None
) -> SuiteKpis:
    executed = [b for b in per_brief if b.executed]
    n = len(executed)
    completed = [b for b in executed if b.completed]
    ttfd = [
        b.time_to_first_deploy_seconds
        for b in completed
        if b.time_to_first_deploy_seconds is not None
    ]
    local_count = sum(1 for b in per_brief if b.local_only)
    cost_measured = bool(executed) and any(b.cost_measured for b in executed)
    coverage_source: CoverageSource | None = None
    if executed:
        coverage_source = (
            "measured" if all(b.coverage_source == "measured" for b in executed) else "declared"
        )
    return SuiteKpis(
        briefs_total=briefs_total,
        briefs_executed=n,
        autonomous_completion_rate=(len(completed) / n if n else 0.0),
        first_draft_rejection_rate=(
            sum(1 for b in executed if b.first_draft_rejected) / n if n else 0.0
        ),
        time_to_first_deploy_seconds=_median([float(t) for t in ttfd]),
        local_only_count=local_count,
        local_only_viability=f"{local_count}/{briefs_total}",
        cost_per_project_usd=_median([b.cost_usd for b in executed]),
        cost_measured=cost_measured,
        generated_test_coverage=_median([b.generated_test_coverage for b in executed]),
        coverage_source=coverage_source,
        crash_recovery_rate=crash_recovery_rate,
        human_intervention_count=sum(b.human_intervention_count for b in executed),
    )


def build_mvp_checks(suite: SuiteKpis) -> list[MvpThreshold]:
    """Evaluate each §7 KPI against its MVP target. Aggregates measured over the
    executed briefs are checked; where nothing was executed the check is ``None``
    (not applicable) rather than a spurious pass or fail."""
    executed = suite.briefs_executed > 0
    ttfd = suite.time_to_first_deploy_seconds
    cost = suite.cost_per_project_usd
    cov = suite.generated_test_coverage
    return [
        MvpThreshold(
            kpi="Autonomous completion rate",
            target="≥ 50%",
            value=f"{suite.autonomous_completion_rate:.0%}",
            met=(suite.autonomous_completion_rate >= MVP_MIN_COMPLETION if executed else None),
        ),
        MvpThreshold(
            kpi="First-draft rejection rate",
            target="≥ 30%",
            value=f"{suite.first_draft_rejection_rate:.0%}",
            met=(
                suite.first_draft_rejection_rate >= MVP_MIN_FIRST_DRAFT_REJECTION
                if executed
                else None
            ),
        ),
        MvpThreshold(
            kpi="Time-to-first-deploy",
            target="≤ 4 h",
            value=(f"{ttfd:.0f}s" if ttfd is not None else "n/a"),
            met=(ttfd <= MVP_MAX_TTFD_SECONDS if ttfd is not None else None),
        ),
        MvpThreshold(
            kpi="Local-only viability",
            target="≥ 3/10",
            value=suite.local_only_viability,
            met=suite.local_only_count >= MVP_MIN_LOCAL_VIABLE,
        ),
        MvpThreshold(
            kpi="Cost per project",
            target="≤ $15",
            # A swallowed offline metering failure leaves $0 UNMEASURED — never scored
            # as a pass. Only a landed gateway call makes cost a real measurement.
            value=(
                f"${cost:.4f}"
                if cost is not None and suite.cost_measured
                else (f"${cost:.4f} (not measured)" if cost is not None else "n/a")
            ),
            met=(cost <= MVP_MAX_COST_USD if (cost is not None and suite.cost_measured) else None),
        ),
        MvpThreshold(
            kpi="Generated test coverage",
            target="≥ 60%",
            # Declared from the fixture under the mock profile; the real MEASURED
            # coverage comes from the Phase-56 offline run. Declared ⇒ displayed, not a pass.
            value=(
                f"{cov:.0%}"
                if cov is not None and suite.coverage_source == "measured"
                else (f"{cov:.0%} (declared)" if cov is not None else "n/a")
            ),
            met=(
                cov >= MVP_MIN_COVERAGE
                if (cov is not None and suite.coverage_source == "measured")
                else None
            ),
        ),
        MvpThreshold(
            kpi="Crash recovery",
            target="100% (simulated in-process)",
            value=(
                f"{suite.crash_recovery_rate:.0%}"
                if suite.crash_recovery_rate is not None
                else "n/a"
            ),
            met=(
                suite.crash_recovery_rate >= MVP_CRASH_RECOVERY
                if suite.crash_recovery_rate is not None
                else None
            ),
        ),
        MvpThreshold(
            kpi="Human intervention count",
            target="displayed (not driven to zero)",
            value=str(suite.human_intervention_count),
            met=None,
        ),
    ]


__all__ = [
    "BriefKpis",
    "CoverageSource",
    "KpiReport",
    "MvpThreshold",
    "SuiteKpis",
    "build_mvp_checks",
    "build_suite_kpis",
]
