"""Phase 74 (Vol 8 §11): the TITAN-SEC-100 scan-exception registry enforcement.

DoD: the checked-in registry is clean; and the check BITES — an expired,
too-far-future, malformed, or unknown-tool exception each fails the build. `today`
is injected so the expiry logic is deterministic.

Pure: no DB, no network.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import yaml
from scripts.check_scan_exceptions import EXCEPTIONS_PATH, collect_errors

pytestmark: list[Any] = []

_TODAY = date(2026, 7, 29)


def _entry(**over: object) -> dict[str, Any]:
    base = {
        "tool": "pip-audit",
        "id": "CVE-2099-0001",
        "reason": "no upstream fix; not reachable from our call paths",
        "owner": "security",
        "expires": date(2026, 8, 15),
    }
    base.update(over)
    return base


def test_live_registry_is_clean() -> None:
    doc = yaml.safe_load(EXCEPTIONS_PATH.read_text("utf-8")) or {}
    assert collect_errors(doc, date.today()) == []


def test_valid_future_exception_passes() -> None:
    assert collect_errors({"exceptions": [_entry()]}, _TODAY) == []


def test_expired_exception_is_flagged() -> None:
    errors = collect_errors({"exceptions": [_entry(expires=date(2026, 7, 1))]}, _TODAY)
    assert any("EXPIRED" in e for e in errors), errors


def test_too_far_future_is_flagged() -> None:
    errors = collect_errors({"exceptions": [_entry(expires=date(2027, 1, 1))]}, _TODAY)
    assert any("more than" in e for e in errors), errors


def test_missing_field_and_unknown_tool_are_flagged() -> None:
    errors = collect_errors(
        {"exceptions": [{"tool": "made-up-scanner", "id": "X", "expires": date(2026, 8, 1)}]},
        _TODAY,
    )
    assert any("missing required field 'reason'" in e for e in errors), errors
    assert any("unknown tool" in e for e in errors), errors


def test_bad_date_string_is_flagged() -> None:
    errors = collect_errors({"exceptions": [_entry(expires="not-a-date")]}, _TODAY)
    assert any("not a YYYY-MM-DD date" in e for e in errors), errors
