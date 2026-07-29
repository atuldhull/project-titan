"""CI enforcement for the security-scan exception registry (TITAN-SEC-100).

`security/scan-exceptions.yaml` is the ONLY sanctioned way to ship past a
HIGH/CRITICAL scanner finding, and every entry is time-boxed. This check fails the
build when any exception is:

  * malformed (missing a required field, or an unknown `tool`);
  * undated / mis-dated (`expires` not a YYYY-MM-DD date);
  * already EXPIRED (its `expires` date has passed — the finding must be re-fixed
    or the exception re-justified); or
  * dated more than `MAX_HORIZON_DAYS` in the future (no de-facto permanent
    suppressions).

Pure filesystem + YAML — no DB, no network. Exit 0 clean, 1 on any violation.
The `today` is injected for the unit test; production passes the real date.

Usage: uv run python scripts/check_scan_exceptions.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
EXCEPTIONS_PATH = REPO_ROOT / "security" / "scan-exceptions.yaml"

_REQUIRED = ("tool", "id", "reason", "owner", "expires")
_VALID_TOOLS = {"bandit", "pip-audit", "npm-audit", "osv-scanner", "trivy", "semgrep"}
MAX_HORIZON_DAYS = 90


def collect_errors(doc: dict[str, Any], today: date) -> list[str]:
    errors: list[str] = []
    entries = doc.get("exceptions")
    if entries is None:
        return ["scan-exceptions: top-level `exceptions:` key is missing"]
    if not isinstance(entries, list):
        return ["scan-exceptions: `exceptions:` must be a list"]

    for i, entry in enumerate(entries):
        tag = f"exceptions[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{tag}: not a mapping")
            continue
        for field in _REQUIRED:
            if not entry.get(field):
                errors.append(f"{tag}: missing required field '{field}'")
        tool = entry.get("tool")
        if tool is not None and tool not in _VALID_TOOLS:
            errors.append(f"{tag}: unknown tool '{tool}' (allowed: {sorted(_VALID_TOOLS)})")

        expires = entry.get("expires")
        if expires is None:
            continue  # already reported as missing
        # PyYAML parses an unquoted YYYY-MM-DD as a datetime.date already.
        if isinstance(expires, date):
            exp = expires
        else:
            try:
                exp = date.fromisoformat(str(expires))
            except ValueError:
                errors.append(f"{tag}: `expires` '{expires}' is not a YYYY-MM-DD date")
                continue
        ident = entry.get("id", "?")
        if exp < today:
            errors.append(
                f"{tag}: exception for {tool}:{ident} EXPIRED on {exp} — re-fix the "
                "finding or re-justify with a new date (TITAN-SEC-100)"
            )
        elif (exp - today).days > MAX_HORIZON_DAYS:
            errors.append(
                f"{tag}: exception for {tool}:{ident} expires {exp}, more than "
                f"{MAX_HORIZON_DAYS} days out — exceptions must be short-lived"
            )
    return errors


def main() -> None:
    if not EXCEPTIONS_PATH.exists():
        raise SystemExit(f"scan-exceptions: missing {EXCEPTIONS_PATH}")
    doc = yaml.safe_load(EXCEPTIONS_PATH.read_text(encoding="utf-8")) or {}
    errors = collect_errors(doc, date.today())
    if errors:
        sys.stderr.write("scan-exceptions check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)
    n = len(doc.get("exceptions") or [])
    sys.stdout.write(f"scan-exceptions OK: {n} active exception(s), none expired.\n")


if __name__ == "__main__":
    main()
