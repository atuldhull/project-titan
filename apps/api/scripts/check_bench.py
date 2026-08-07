"""CI gate: an EP that changes evolution behavior must ship a passing TITAN-Bench report
(Phase 86, Vol 10 §6, TITAN-EVO-017 / ADR-EVO-02).

The offline half of the evolution loop: no Evolution Proposal may enter online A/B
until its challenger matches or beats the champion on every bench track. This gate
fails the build (exit 1) when an EP under review changes a behavior-affecting evolution
artifact (`evolution/prompts/**`, `evolution/routing/**`, or `evolution/manifest.yaml`)
but either (a) attaches no challenger bench report, (b) attaches a report with no
manifest_sha (not a real run) or with the champion baseline missing, or (c) attaches one
that is inferior to the champion on any track per `evolution/bench/gates.yaml`
(non-inferiority margin + absolute floor). Mirrors check_evolution.py: pure
`collect_errors` + thin main.

Convention: the EP author runs `titan bench run` on the challenger and commits the
report to `evolution/bench/reports/challenger.json`; the champion baseline lives at
`evolution/bench/reports/champion.json`. The gate validates the report is present,
well-formed, and non-inferior; the SCORES' authenticity relies on the EP-branch commit +
human review (CI cannot re-run the bench without models) — the manifest's trust model.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan.evolution.bench import BenchReport, evaluate_gate, load_gates  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


# Any of these paths changing is a behavior-affecting Evolution Proposal that must be
# benchmarked — NOT just prompt bodies: the manifest pins which prompt SHA is live, and the
# routing policy selects which model serves each agent (review finding gate-3).
_BENCH_TRIGGER_PREFIXES = ("evolution/prompts/", "evolution/routing/", "evolution/manifest.yaml")


def _evolution_behavior_changed() -> bool:
    """True if the PR changes any behavior-affecting evolution artifact vs the base branch.

    Best-effort: if git is unavailable we assume a report IS required (fail-safe — a gate
    that can't tell must not wave the EP through). CI should use a FULL checkout so the
    origin/main base resolves; a shallow HEAD~1 fallback may miss an earlier commit's edit."""
    for base in ("origin/main", "main", "HEAD~1"):
        try:
            out = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "diff", "--name-only", base, "--"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        if out.returncode == 0:
            return any(line.startswith(_BENCH_TRIGGER_PREFIXES) for line in out.stdout.splitlines())
    return True


def collect_errors(repo_root: Path = REPO_ROOT, *, require_report: bool | None = None) -> list[str]:
    errors: list[str] = []
    gates_path = repo_root / "evolution" / "bench" / "gates.yaml"
    if not gates_path.exists():
        return ["evolution/bench/gates.yaml is missing"]
    gates = load_gates(repo_root)
    if "suite_version" not in gates or "tracks" not in gates:
        return ["gates.yaml is malformed (needs suite_version + tracks)"]

    challenger_path = repo_root / "evolution" / "bench" / "reports" / "challenger.json"
    required = require_report if require_report is not None else _evolution_behavior_changed()

    if not challenger_path.exists():
        if required:
            errors.append(
                "an EP changes a behavior-affecting evolution artifact but no challenger bench "
                "report is attached at evolution/bench/reports/challenger.json (run `titan bench "
                "run`)"
            )
        return errors  # nothing to gate if no EP is under review

    try:
        challenger = BenchReport.from_dict(json.loads(challenger_path.read_text("utf-8")))
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return [f"challenger.json is not a valid bench report: {exc}"]

    # A blank/hand-stubbed report with no manifest_sha cannot be trusted as a real run. The
    # SCORES' authenticity still relies on the EP-branch commit + human review (CI cannot
    # re-run the bench without models) — the same trust model as the git-versioned manifest.
    if not challenger.manifest_sha:
        errors.append("challenger.json has no manifest_sha (not a real `titan bench run`)")

    # The champion baseline MUST be present, else the non-inferiority bar silently collapses
    # to the absolute floors (a challenger could regress to the floor and still pass).
    champion_path = repo_root / "evolution" / "bench" / "reports" / "champion.json"
    if not champion_path.exists():
        errors.append("champion.json (the non-inferiority baseline) is missing")
        return errors
    try:
        champion = BenchReport.from_dict(json.loads(champion_path.read_text("utf-8")))
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return [f"champion.json is present but invalid: {exc}"]

    outcome = evaluate_gate(challenger, gates, champion=champion)
    if not outcome.passed:
        errors.append("challenger bench report fails the offline gate:")
        errors.extend(f"    {f}" for f in outcome.failures)
    return errors


def main() -> None:
    errors = collect_errors()
    if errors:
        print("bench gate FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(1)
    print("bench gate OK: no unbenchmarked prompt change (or the challenger passes gates.yaml)")


if __name__ == "__main__":
    main()
