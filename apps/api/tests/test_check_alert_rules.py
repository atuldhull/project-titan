"""Phase 77 (Vol 9 §7, OPS-017): the alert-rules validation gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.check_alert_rules import RULES_DIR, _rule_files, collect_errors

pytestmark: list[Any] = []


def test_live_rules_are_valid() -> None:
    assert collect_errors(_rule_files(RULES_DIR)) == []


def _write(tmp: Path, name: str, body: str) -> Path:
    (tmp / "rules").mkdir(exist_ok=True)
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


def test_paging_alert_without_runbook_url_is_flagged(tmp_path: Path) -> None:
    body = (
        "groups:\n  - name: g\n    rules:\n"
        "      - alert: Boom\n        expr: up == 0\n"
        "        labels: {severity: page}\n        annotations: {summary: down}\n"
    )
    assert any("runbook_url" in e for e in collect_errors([_write(tmp_path, "x.yml", body)]))


def test_paging_alert_with_runbook_url_is_ok(tmp_path: Path) -> None:
    body = (
        "groups:\n  - name: g\n    rules:\n"
        "      - alert: Boom\n        expr: up == 0\n"
        "        labels: {severity: page}\n"
        "        annotations: {summary: down, runbook_url: http://r/x}\n"
    )
    assert collect_errors([_write(tmp_path, "x.yml", body)]) == []


def test_missing_multiwindow_burn_alert_is_flagged(tmp_path: Path) -> None:
    body = (
        "groups:\n  - name: g\n    rules:\n"
        "      - alert: APIErrorBudgetFastBurn\n        expr: ratio > 0.01\n"
        "        labels: {severity: page}\n"
        "        annotations: {summary: burn, runbook_url: http://r/x}\n"
    )
    # single-window expr (no `and`) → not multiwindow → flagged
    assert any("multiwindow" in e for e in collect_errors([_write(tmp_path, "x.yml", body)]))
