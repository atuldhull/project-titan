"""CI gate for the Prometheus alert rules (Phase 77, Vol 9 §7, TITAN-OPS-017).

Structurally validates every alert rule under infra/prometheus/ so a rule can never
ship malformed: each alert needs an `expr`, a `labels.severity`, and an
`annotations.summary`; every `severity: page` MUST carry an `annotations.runbook_url`
(OPS-017 "every page MUST link to its runbook section"); and the availability/latency
SLO burn-rate alerts MUST be multiwindow (an `and`-joined multi-window expr), never a
static threshold. A promtool syntax check runs in CI on top of this.

Pure filesystem + YAML — no DB, no network. Exit 0 clean, 1 on any violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

_AND_OP = re.compile(r"\band\b")  # the PromQL set operator, at a line start or inline
# A window either as an inline range vector ([1h]/[5m]) OR baked into a recording-rule
# name (…:rate1h / …rate30m) — the burn-rate alerts AND two pre-recorded windows.
_WINDOW = re.compile(r"\[(\d+[smhdwy])\]|rate(\d+[smhdwy])\b")
_SEVERITIES = {"page", "ticket", "warning", "info"}  # page/ticket route; warning/info legacy

RULES_DIR = Path(__file__).resolve().parents[3] / "infra" / "prometheus"
# Availability/latency SLO burn-rate alerts that MUST be multiwindow (OPS-017).
_MULTIWINDOW_REQUIRED = {"APIErrorBudgetFastBurn", "APIErrorBudgetSlowBurn"}


def _rule_files(base: Path) -> list[Path]:
    files = sorted(base.glob("*.yml")) + sorted((base / "rules").glob("*.yml"))
    return [p for p in files if p.is_file()]


def collect_errors(files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for group in doc.get("groups") or []:
            gname = group.get("name", "<no-name>")
            for rule in group.get("rules") or []:
                if "record" in rule:
                    if not rule.get("expr"):
                        errors.append(f"{path.name}/{gname}: recording rule missing expr")
                    continue
                alert = rule.get("alert")
                if not alert:
                    errors.append(f"{path.name}/{gname}: rule is neither `alert` nor `record`")
                    continue
                _check_alert(errors, path.name, alert, rule)
    return errors


def missing_required_alerts(files: list[Path]) -> list[str]:
    """The required multiwindow SLO alerts that must exist SOMEWHERE (a repo-wide
    check, separate from the per-rule structural checks)."""
    present: set[str] = set()
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for group in doc.get("groups") or []:
            for rule in group.get("rules") or []:
                if rule.get("alert"):
                    present.add(rule["alert"])
    missing = sorted(_MULTIWINDOW_REQUIRED - present)
    return [f"required multiwindow SLO alert '{r}' is missing" for r in missing]


def _check_alert(errors: list[str], fname: str, alert: str, rule: dict[str, Any]) -> None:
    expr = str(rule.get("expr") or "")
    labels = rule.get("labels") or {}
    annotations = rule.get("annotations") or {}
    if not expr:
        errors.append(f"{fname}: alert {alert} missing expr")
    severity = labels.get("severity")
    if severity not in _SEVERITIES:
        errors.append(
            f"{fname}: alert {alert} bad severity '{severity}' (need {sorted(_SEVERITIES)})"
        )
    if not annotations.get("summary"):
        errors.append(f"{fname}: alert {alert} missing annotations.summary")
    if severity == "page" and not annotations.get("runbook_url"):
        errors.append(f"{fname}: paging alert {alert} has no runbook_url (OPS-017)")
    if alert in _MULTIWINDOW_REQUIRED:
        # Multiwindow = an `and`-joined expr over TWO DISTINCT windows (e.g. 1h AND 5m),
        # not a static threshold and not one window repeated (review #7).
        windows = {a or b for a, b in _WINDOW.findall(expr)}
        if not _AND_OP.search(expr) or len(windows) < 2:
            errors.append(
                f"{fname}: burn-rate alert {alert} is not multiwindow — needs an `and`-joined "
                f"expr over two distinct windows (saw {sorted(windows) or 'none'}) (OPS-017)"
            )


def main() -> None:
    files = _rule_files(RULES_DIR)
    if not files:
        raise SystemExit(f"check-alert-rules: no rule files under {RULES_DIR}")
    errors = collect_errors(files) + missing_required_alerts(files)
    if errors:
        sys.stderr.write("alert-rules check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)
    sys.stdout.write(
        f"alert-rules OK: {len(files)} rule file(s), every page carries a runbook_url.\n"
    )


if __name__ == "__main__":
    main()
