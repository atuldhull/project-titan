"""CI/DR gate for a disaster-recovery drill report (Phase 81, Vol 9 §8.3, TITAN-OPS-019).

`ops/scripts/dr-drill.sh` writes a JSON report into `ops/dr-drills/` after a cluster-loss
restore rehearsal. This validates that report so a drill can't be quietly marked PASS while
missing a step or blowing the RTO/RPO budget (the Phase-78 "decorative verification" lesson —
a drill that LOGS success proves nothing; the report must show every §8.3 step completed and
the MEASURED recovery inside the target):

1. All eight §8.3 restore steps are present, in order, each with ok status + start/end times.
2. Timestamps are monotonic (a step can't finish before it starts / before the prior step).
3. MEASURED rto <= target rto (<= 4 h) and MEASURED rpo <= target rpo (<= 5 min), OPS-019.
4. result == "PASS" iff every step is ok AND both budgets are met (no lying header).

Pure JSON — no cluster. `collect_errors(report)` is the testable core. Exit 0 clean, 1 on any
violation; `main` validates the newest report under ops/dr-drills/ (or a path argument).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[3]
DRILLS_DIR = _ROOT / "ops" / "dr-drills"

# The §8.3 restore ORDER (TITAN-OPS-019) — names must match dr-drill.sh exactly and appear
# in this sequence. Postgres before requeue (the ledger drives re-enqueue); deploy before smoke;
# DNS flip last (no traffic until the stack is proven).
REQUIRED_STEPS = [
    "provision",  # 1: cluster from infra/ (Terraform + kustomize)
    "restore-secrets",  # 2
    "restore-postgres-pitr",  # 3: WAL PITR to latest
    "requeue-from-ledger",  # 4: start Redis empty, re-enqueue non-terminal ledger rows
    "restore-qdrant",  # 5: snapshots
    "deploy-lkg-digests",  # 6: last-known-good digests from the release manifest
    "smoke",  # 7: smoke suite green in the restored cluster
    "flip-dns",  # 8
]
RTO_TARGET_SECONDS = 4 * 60 * 60  # OPS-019: RTO <= 4 h
RPO_TARGET_SECONDS = 5 * 60  # OPS-019: RPO <= 5 min


def _parse_ts(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    steps = report.get("steps")
    if not isinstance(steps, list):
        return ["report has no 'steps' list"]

    got_names = [s.get("name") if isinstance(s, dict) else None for s in steps]
    if got_names != REQUIRED_STEPS:
        errors.append(
            f"steps {got_names} != the §8.3 order {REQUIRED_STEPS} "
            f"(missing/extra/reordered restore steps)"
        )

    # 1 + 2: each step ok + monotonic timestamps (within a step and across the sequence).
    prev_end: dt.datetime | None = None
    all_ok = True
    for step in steps:
        if not isinstance(step, dict):
            errors.append(f"step is not an object: {step!r}")
            all_ok = False
            continue
        name = step.get("name", "<no-name>")
        if step.get("status") != "ok":
            errors.append(f"step '{name}': status {step.get('status')!r} != 'ok'")
            all_ok = False
        start, end = _parse_ts(step.get("started_at")), _parse_ts(step.get("completed_at"))
        if start is None or end is None:
            errors.append(f"step '{name}': missing/invalid started_at/completed_at")
            continue
        if end < start:
            errors.append(f"step '{name}': completed_at is before started_at")
        if prev_end is not None and start < prev_end:
            errors.append(f"step '{name}': starts before the previous step finished (out of order)")
        prev_end = end

    # 3: measured RTO/RPO within budget.
    measured = report.get("measured") or {}
    rto, rpo = measured.get("rto_seconds"), measured.get("rpo_seconds")
    if not isinstance(rto, (int, float)):
        errors.append("measured.rto_seconds missing")
    elif rto > RTO_TARGET_SECONDS:
        errors.append(
            f"measured RTO {rto}s exceeds the {RTO_TARGET_SECONDS}s (4 h) target (OPS-019)"
        )
    if not isinstance(rpo, (int, float)):
        errors.append("measured.rpo_seconds missing")
    elif rpo > RPO_TARGET_SECONDS:
        errors.append(
            f"measured RPO {rpo}s exceeds the {RPO_TARGET_SECONDS}s (5 min) target (OPS-019)"
        )

    # 4: the header result must not claim PASS unless every step ok AND both budgets met.
    budgets_ok = (
        isinstance(rto, (int, float))
        and rto <= RTO_TARGET_SECONDS
        and isinstance(rpo, (int, float))
        and rpo <= RPO_TARGET_SECONDS
    )
    should_pass = all_ok and budgets_ok and got_names == REQUIRED_STEPS
    result = report.get("result")
    if result == "PASS" and not should_pass:
        errors.append(
            "result is 'PASS' but a step failed or a budget was exceeded (dishonest header)"
        )
    if result not in ("PASS", "FAIL"):
        errors.append(f"result {result!r} must be 'PASS' or 'FAIL'")
    return errors


def _newest_report(directory: Path) -> Path | None:
    reports = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return reports[-1] if reports else None


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    path = Path(arg) if arg else _newest_report(DRILLS_DIR)
    if path is None or not path.is_file():
        raise SystemExit(f"check-dr-report: no report found (looked in {DRILLS_DIR})")
    report = json.loads(path.read_text(encoding="utf-8"))
    errors = collect_errors(report)
    if errors:
        sys.stderr.write(f"DR drill report {path.name} FAILED validation:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)
    sys.stdout.write(
        f"DR report {path.name} OK: all 8 §8.3 steps completed; RTO/RPO within 4h/5min targets.\n"
    )


if __name__ == "__main__":
    main()
