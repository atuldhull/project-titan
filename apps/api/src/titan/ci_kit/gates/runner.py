"""Run every constitutional gate over a workspace and report once.

This module is the single entry point for the generated project's CI workflow
AND for the kit's own fixture tests, so the fixtures verify literally the code
that runs in CI — not a re-implementation of it that can drift away from the
thing it is supposed to be pinning.

Three outcomes, deliberately distinct:

* **PASS** — the gate ran and found nothing.
* **FAIL** — the gate ran and found violations.
* **ERROR** — the gate could not run (Ruff missing, no coverage.xml, corrupt
  baseline). This exits non-zero like a FAIL, because a gate that did not run has
  not been satisfied. Collapsing ERROR into PASS is how enforcement kits quietly
  stop enforcing; collapsing it into FAIL would send the agent hunting for a
  violation that does not exist.
"""

import argparse
import subprocess  # noqa: S404 — fixed argv, no shell
import sys
from dataclasses import dataclass
from pathlib import Path

from titan.ci_kit.config import ConfigError, load_config
from titan.ci_kit.gates import (
    commit_msg,
    diff_coverage,
    hermeticity,
    layering,
    log_scan,
    migration_sql,
    naming,
    orm_boundary,
)
from titan.ci_kit.gates.outcome import Finding, GateOutcome
from titan.ci_kit.spec import BY_GATE

# The gates a workspace scan runs. commit_msg is handled separately: its subject
# is a git history, not a tree.
_WORKSPACE_GATES = (layering, orm_boundary, naming, migration_sql, log_scan, hermeticity)


@dataclass(frozen=True)
class GateReport:
    """One gate's result, including the case where it could not run."""

    gate: str
    wfl_id: str
    outcome: GateOutcome | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == "" and self.outcome is not None and self.outcome.ok

    def render(self) -> str:
        if self.error:
            return f"ERROR {self.gate} ({self.wfl_id}): {self.error}"
        assert self.outcome is not None
        return self.outcome.render()


def _commit_messages(workspace: Path, base_ref: str) -> list[str] | None:
    """Commit messages on this branch since the merge-base, or None if the
    workspace is not a git repo / the base ref is unknown."""
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "log", "--reverse", "--format=%B%x00", f"{base_ref}..HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        return None
    return [chunk.strip() for chunk in result.stdout.split("\0") if chunk.strip()]


def _run_commit_msg(workspace: Path, base_ref: str) -> GateReport:
    rule = BY_GATE["commit_msg"]
    messages = _commit_messages(workspace, base_ref)
    if messages is None:
        return GateReport(
            gate=rule.gate,
            wfl_id=rule.wfl_id,
            outcome=None,
            error=f"not a git repository, or base ref {base_ref!r} is unknown",
        )
    findings: list[Finding] = []
    for message in messages:
        outcome = commit_msg.check_message(message, path="git log")
        findings.extend(outcome.findings)
    if findings:
        return GateReport(
            gate=rule.gate,
            wfl_id=rule.wfl_id,
            outcome=GateOutcome(
                gate=rule.gate, wfl_id=rule.wfl_id, ok=False, findings=tuple(findings)
            ),
        )
    return GateReport(
        gate=rule.gate,
        wfl_id=rule.wfl_id,
        outcome=GateOutcome(gate=rule.gate, wfl_id=rule.wfl_id, ok=True),
    )


def run_gates(
    workspace: Path,
    *,
    only: tuple[str, ...] = (),
    update_baseline: bool = False,
) -> list[GateReport]:
    """Run the gates (all of them, or just ``only``) over ``workspace``."""
    config = load_config(workspace)
    reports: list[GateReport] = []

    def wanted(gate: str) -> bool:
        return not only or gate in only

    for module in _WORKSPACE_GATES:
        if not wanted(module.GATE):
            continue
        try:
            reports.append(
                GateReport(gate=module.GATE, wfl_id=module.WFL_ID, outcome=module.check(workspace))
            )
        except Exception as exc:  # noqa: BLE001 — a broken gate is an ERROR, never a pass
            reports.append(
                GateReport(gate=module.GATE, wfl_id=module.WFL_ID, outcome=None, error=str(exc))
            )

    if wanted("commit_msg"):
        reports.append(_run_commit_msg(workspace, config.base_ref))

    if wanted("diff_coverage"):
        try:
            reports.append(
                GateReport(
                    gate=diff_coverage.GATE,
                    wfl_id=diff_coverage.WFL_ID,
                    outcome=diff_coverage.check(workspace, update_baseline=update_baseline),
                )
            )
        except Exception as exc:  # noqa: BLE001
            reports.append(
                GateReport(
                    gate=diff_coverage.GATE,
                    wfl_id=diff_coverage.WFL_ID,
                    outcome=None,
                    error=str(exc),
                )
            )

    return reports


def render(reports: list[GateReport]) -> str:
    lines = ["", "The Engineering Standards Constitution — Vol 6 §6", "=" * 52]
    lines.extend(report.render() for report in reports)
    failing = [report for report in reports if not report.ok]
    lines.append("=" * 52)
    if failing:
        gates = ", ".join(f"{r.gate} ({r.wfl_id})" for r in failing)
        lines.append(f"REJECTED — {len(failing)} of {len(reports)} gates failing: {gates}")
    else:
        lines.append(f"OK — all {len(reports)} constitutional gates pass")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the constitutional CI gates (Vol 6 §6)")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--gate",
        action="append",
        default=[],
        choices=sorted(BY_GATE),
        help="run only this gate (repeatable); default runs all",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record the project-wide coverage floor (run on the base branch)",
    )
    args = parser.parse_args(argv)

    try:
        reports = run_gates(
            args.workspace,
            only=tuple(args.gate),
            update_baseline=args.update_baseline,
        )
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        return 2

    print(render(reports))
    return 0 if all(report.ok for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
