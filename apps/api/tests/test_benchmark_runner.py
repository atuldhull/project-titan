"""The Section-7 KPI benchmark runner end-to-end (Phase 55 DoD item 3). Runs the
one executable brief to release, folds in the chaos suite, and emits a complete
KPI report — every §7 metric present and sane. Schema-marked (test DB) + a
metered mock gateway so the cost KPI reads a real gateway ledger figure."""

from pathlib import Path
from typing import Any

import pytest

from tests.benchmark_helpers import metered_gateway
from tests.conftest import run_async
from titan.benchmark.runner import BenchmarkRunner
from titan.handoffs import gates

pytestmark = pytest.mark.schema


@pytest.fixture
def _gates_isolation() -> Any:
    """The runner mutates the process-global gate registry; snapshot + restore it
    (not clear-to-default, which would auto-pass a co-running test's gates)."""
    saved = gates.snapshot_gates()
    yield
    gates.restore_gates(saved)


def test_runner_emits_complete_kpi_report(
    migrated_engine: Any, tmp_path: Path, _gates_isolation: Any
) -> None:
    async def scenario() -> None:
        gateway = metered_gateway(migrated_engine, tmp_path)
        try:
            runner = BenchmarkRunner(migrated_engine, gateway=gateway)
            report = await runner.run(profile="mock", run_id="bench-test")
        finally:
            await gateway.aclose()

        # The suite: ten briefs, exactly one executed end-to-end (scope control).
        assert len(report.per_brief) == 10
        executed = [b for b in report.per_brief if b.executed]
        assert len(executed) == 1 and executed[0].brief_id == "todo-tiny"

        brief = executed[0]
        assert brief.completed, "the brief ran to a released pipeline"
        assert brief.first_draft_rejected, "the induced G2 failure reopened the draft"
        assert brief.time_to_first_deploy_seconds is not None
        assert brief.cost_usd > 0, "gateway-tracked spend recorded in llm_usage"
        assert brief.human_intervention_count > 0, "displayed, never zero"
        assert brief.generated_test_coverage >= 0.6

        # Every §7 aggregate is present and measured.
        suite = report.suite
        assert suite.briefs_total == 10 and suite.briefs_executed == 1
        assert suite.autonomous_completion_rate == 1.0
        assert suite.first_draft_rejection_rate == 1.0
        assert suite.time_to_first_deploy_seconds is not None
        assert suite.local_only_count >= 3 and suite.local_only_viability.endswith("/10")
        assert suite.cost_per_project_usd is not None and suite.cost_per_project_usd > 0
        assert suite.generated_test_coverage is not None
        assert suite.crash_recovery_rate == 1.0  # the chaos sweep passed 5/5
        assert suite.human_intervention_count > 0

        # The MVP check covers all eight §7 KPIs and no measured threshold failed.
        assert {t.kpi for t in report.mvp} == {
            "Autonomous completion rate",
            "First-draft rejection rate",
            "Time-to-first-deploy",
            "Local-only viability",
            "Cost per project",
            "Generated test coverage",
            "Crash recovery",
            "Human intervention count",
        }
        assert report.meets_mvp
        # The human-intervention KPI is displayed, never a pass/fail gate.
        human = next(t for t in report.mvp if t.kpi == "Human intervention count")
        assert human.met is None
        # Coverage is fixture-DECLARED under the mock profile → displayed, not a pass.
        coverage = next(t for t in report.mvp if t.kpi == "Generated test coverage")
        assert coverage.met is None and "declared" in coverage.value
        # Cost WAS measured (the metered mock gateway landed) → a real pass.
        cost = next(t for t in report.mvp if t.kpi == "Cost per project")
        assert cost.met is True and report.suite.cost_measured

    run_async(scenario)


def test_runner_rejects_a_fixture_brief(migrated_engine: Any, _gates_isolation: Any) -> None:
    async def scenario() -> None:
        runner = BenchmarkRunner(migrated_engine)
        with pytest.raises(ValueError, match="fixture"):
            await runner.run(brief_id="url-shortener")

    run_async(scenario)
