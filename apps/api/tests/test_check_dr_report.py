"""Phase 81 (Vol 9 §8.3, OPS-019): the DR-drill report gate. Pins a valid report AND re-breaks
each invariant — a missing/reordered step, a failed step, a blown RTO/RPO budget, non-monotonic
timestamps, and a PASS header over a failed drill (the dishonest-success case)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from scripts.check_dr_report import REQUIRED_STEPS, collect_errors

pytestmark: list[Any] = []

_BASE = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _valid_report() -> dict[str, Any]:
    steps = []
    for i, name in enumerate(REQUIRED_STEPS):
        start = _BASE + dt.timedelta(minutes=i * 2)
        end = start + dt.timedelta(minutes=1)
        steps.append(
            {
                "name": name,
                "started_at": start.isoformat(),
                "completed_at": end.isoformat(),
                "status": "ok",
            }
        )
    return {
        "drill_id": "dr-1-staging",
        "environment": "staging",
        "started_at": _BASE.isoformat(),
        "completed_at": (_BASE + dt.timedelta(minutes=16)).isoformat(),
        "targets": {"rto_seconds": 14400, "rpo_seconds": 300},
        "measured": {"rto_seconds": 960, "rpo_seconds": 60},
        "steps": steps,
        "result": "PASS",
    }


def test_valid_report_passes() -> None:
    assert collect_errors(_valid_report()) == []


def test_missing_step_is_flagged() -> None:
    r = _valid_report()
    r["steps"] = [s for s in r["steps"] if s["name"] != "restore-postgres-pitr"]
    assert any("§8.3 order" in e for e in collect_errors(r))


def test_reordered_steps_are_flagged() -> None:
    r = _valid_report()
    r["steps"][0], r["steps"][1] = r["steps"][1], r["steps"][0]
    assert any("§8.3 order" in e for e in collect_errors(r))


def test_failed_step_is_flagged() -> None:
    r = _valid_report()
    r["steps"][3]["status"] = "failed"
    errs = collect_errors(r)
    assert any("status" in e and "requeue-from-ledger" in e for e in errs)


def test_rto_over_budget_is_flagged() -> None:
    r = _valid_report()
    r["measured"]["rto_seconds"] = 14401  # > 4 h
    r["result"] = "FAIL"
    assert any("RTO" in e and "exceeds" in e for e in collect_errors(r))


def test_rpo_over_budget_is_flagged() -> None:
    r = _valid_report()
    r["measured"]["rpo_seconds"] = 301  # > 5 min
    r["result"] = "FAIL"
    assert any("RPO" in e and "exceeds" in e for e in collect_errors(r))


def test_nonmonotonic_timestamps_are_flagged() -> None:
    r = _valid_report()
    # make step 5 start before step 4 finished
    r["steps"][4]["started_at"] = _BASE.isoformat()
    assert any("out of order" in e for e in collect_errors(r))


def test_completed_before_started_is_flagged() -> None:
    r = _valid_report()
    s = r["steps"][2]
    s["completed_at"] = (_BASE - dt.timedelta(minutes=10)).isoformat()
    assert any("before started_at" in e for e in collect_errors(r))


def test_pass_header_over_a_failed_step_is_flagged() -> None:
    r = _valid_report()
    r["steps"][6]["status"] = "failed"  # smoke failed …
    r["result"] = "PASS"  # … but the header lies
    assert any("dishonest header" in e for e in collect_errors(r))


def test_bad_result_value_is_flagged() -> None:
    r = _valid_report()
    r["result"] = "MAYBE"
    assert any("must be 'PASS' or 'FAIL'" in e for e in collect_errors(r))
