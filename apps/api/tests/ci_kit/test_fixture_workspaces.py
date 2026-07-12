"""Phase 45 DoD: one fixture workspace per machine rule, each violating exactly
one rule, each failing at exactly the gate its violation targets while passing
every other gate.

The "passing every other gate" half is the one that actually earns its keep. A
gate that fails the layering fixture is easy; a gate suite where the layering
fixture ALSO trips the coverage gate is a suite that cannot tell an agent what it
did wrong, and an agent that is told the wrong rule fixes the wrong thing.
"""

from pathlib import Path

import pytest

from tests.ci_kit.conftest import PACKAGE, Workspace, make_workspace
from titan.ci_kit.gates.runner import GateReport, run_gates
from titan.ci_kit.spec import BY_GATE, RULES


def _reports(workspace: Path) -> dict[str, GateReport]:
    return {report.gate: report for report in run_gates(workspace)}


def _assert_only_failing(workspace: Path, expected_gate: str) -> GateReport:
    """The DoD assertion: exactly one gate fails, and it is the right one."""
    reports = _reports(workspace)
    assert set(reports) == set(BY_GATE), "the runner must report every declared gate"

    failing = sorted(gate for gate, report in reports.items() if not report.ok)
    assert failing == [expected_gate], (
        f"expected only {expected_gate!r} to fail, but got {failing}. "
        + " | ".join(reports[gate].render() for gate in failing)
    )

    report = reports[expected_gate]
    assert report.outcome is not None, f"{expected_gate} errored instead of failing: {report.error}"
    assert report.outcome.findings, "a failing gate must cite what it rejected"
    for finding in report.outcome.findings:
        assert finding.wfl_id == BY_GATE[expected_gate].wfl_id
        assert finding.fix, "§6.6 TITAN-WFL-033: every finding carries a concrete fix"
    return report


def test_clean_workspace_passes_every_gate(clean_workspace: Workspace) -> None:
    reports = _reports(clean_workspace.root)
    failing = [report.render() for report in reports.values() if not report.ok]
    assert not failing, "the compliant baseline must pass every gate:\n" + "\n".join(failing)
    assert set(reports) == set(BY_GATE)


def test_every_declared_rule_has_a_gate_that_runs(clean_workspace: Workspace) -> None:
    # Traceability: a Rule in the spec with no gate behind it is a rule the
    # constitution claims to enforce and does not.
    reports = _reports(clean_workspace.root)
    for rule in RULES:
        assert rule.gate in reports
        assert reports[rule.gate].wfl_id == rule.wfl_id


# --- one fixture per machine rule --------------------------------------------


def test_domain_importing_infrastructure_fails_only_layering(tmp_path: Path) -> None:
    """TITAN-WFL-022 — §6.1's own acceptance test, verbatim."""
    workspace = make_workspace(
        tmp_path,
        feature_files={
            f"src/{PACKAGE}/domain/pricing.py": """
                from shop.domain.order import Order
                from shop.infrastructure.order_repository import SqlOrderRepository


                def price(order: Order, repository: SqlOrderRepository) -> int:
                    _ = repository
                    return order.total_cents
                """,
        },
    )
    report = _assert_only_failing(workspace.root, "layering")
    assert report.outcome is not None
    rendered = " ".join(f.message for f in report.outcome.findings)
    assert "domain" in rendered and "infrastructure" in rendered


def test_sqlalchemy_in_a_router_fails_only_orm_boundary(tmp_path: Path) -> None:
    """TITAN-WFL-025 — the repository pattern, §6.1's grep-gate acceptance test."""
    workspace = make_workspace(
        tmp_path,
        feature_files={
            f"src/{PACKAGE}/interface/order_routes.py": """
                from sqlalchemy import select

                from shop.domain.order import Order


                def list_orders() -> list[Order]:
                    _ = select
                    return []
                """,
        },
    )
    report = _assert_only_failing(workspace.root, "orm_boundary")
    assert report.outcome is not None
    assert "sqlalchemy" in report.outcome.findings[0].message


def test_camel_case_function_fails_only_naming(tmp_path: Path) -> None:
    """TITAN-WFL-027 — §6.2's acceptance test: a camelCase Python function."""
    workspace = make_workspace(
        tmp_path,
        feature_files={
            f"src/{PACKAGE}/application/discounts.py": """
                from shop.domain.order import Order


                def applyDiscount(order: Order) -> Order:
                    return order
                """,
        },
    )
    report = _assert_only_failing(workspace.root, "naming")
    assert report.outcome is not None
    assert "N802" in report.outcome.findings[0].message


def test_non_conventional_commit_fails_only_commit_msg(tmp_path: Path) -> None:
    """TITAN-WFL-027 — §6.2's other acceptance test: a non-conventional message."""
    workspace = make_workspace(
        tmp_path,
        commit_message="updated some stuff",
        feature_files={
            f"src/{PACKAGE}/application/refunds.py": """
                from shop.domain.order import Order


                def refund(order: Order) -> Order:
                    return Order(id=order.id, total_cents=0)
                """,
        },
    )
    report = _assert_only_failing(workspace.root, "commit_msg")
    assert report.outcome is not None
    assert "updated some stuff" in report.outcome.findings[0].message


def test_under_covered_diff_fails_only_diff_coverage(tmp_path: Path) -> None:
    """TITAN-WFL-030 — §6.5's acceptance test: a PR below the diff-coverage gate."""
    path = f"src/{PACKAGE}/application/invoices.py"
    workspace = make_workspace(
        tmp_path,
        feature_files={
            path: """
                from shop.domain.order import Order


                def invoice_total(order: Order, tax_percent: int) -> int:
                    tax = order.total_cents * tax_percent // 100
                    return order.total_cents + tax
                """,
        },
        # Every added executable line unhit: 0% line coverage on the diff.
        coverage={path: {line: (0, 0, 0) for line in range(1, 7)}},
    )
    report = _assert_only_failing(workspace.root, "diff_coverage")
    assert report.outcome is not None
    assert "line coverage" in report.outcome.findings[0].message


def test_bad_migration_naming_fails_only_migration_sql(tmp_path: Path) -> None:
    """TITAN-WFL-027 — the §6.2 database row: singular table, mis-named index."""
    workspace = make_workspace(
        tmp_path,
        feature_files={
            "migrations/0002_invoice.sql": """
                CREATE TABLE invoice (
                    id UUID PRIMARY KEY,
                    order_id UUID NOT NULL REFERENCES orders (id)
                );

                CREATE INDEX invoice_order_idx ON invoice (order_id);
                """,
        },
    )
    report = _assert_only_failing(workspace.root, "migration_sql")
    assert report.outcome is not None
    messages = " ".join(f.message for f in report.outcome.findings)
    assert "singular" in messages
    assert "ix_invoice_" in messages


# --- the never-decrease half of TITAN-WFL-030 --------------------------------


def test_project_coverage_may_never_decrease(tmp_path: Path) -> None:
    """§6.5: diff coverage alone is gameable by deleting tests for untouched code.

    The diff here is fully covered — the ONLY thing wrong is that project-wide
    coverage fell below the recorded floor.
    """
    path = f"src/{PACKAGE}/application/tidy.py"
    workspace = make_workspace(
        tmp_path,
        feature_files={
            path: """
                def tidy(value: str) -> str:
                    return value.strip()
                """,
        },
        coverage={path: {1: (1, 0, 0), 2: (1, 0, 0)}},
        project_rate=71.0,
        baseline=88.0,
    )
    report = _assert_only_failing(workspace.root, "diff_coverage")
    assert report.outcome is not None
    assert "never decrease" in report.outcome.findings[0].fix
    assert "88.00% to 71.00%" in report.outcome.findings[0].message


def test_missing_coverage_report_is_an_error_not_a_pass(clean_workspace: Workspace) -> None:
    """A gate that could not run has NOT been satisfied.

    If an absent coverage.xml read as a pass, deleting the test step from CI
    would silently disable TITAN-WFL-030 — the single highest-value thing an
    agent under schedule pressure could do to make a red build go green.
    """
    (clean_workspace.root / "coverage.xml").unlink()

    report = _reports(clean_workspace.root)["diff_coverage"]
    assert not report.ok
    assert report.outcome is None, "a gate that cannot run must ERROR, not FAIL"
    assert "coverage.xml" in report.error


@pytest.mark.parametrize("rule", RULES, ids=lambda rule: rule.gate)
def test_every_rule_cites_a_real_wfl_requirement(rule: object) -> None:
    from titan.ci_kit.spec import Rule

    assert isinstance(rule, Rule)
    assert rule.wfl_id.startswith("TITAN-WFL-")
    assert rule.title and rule.mechanism
