"""CI consistency check for the findings register (TITAN-SEC-131, Vol 8 §14.4).

Every incident / pen-test / adversarial-review finding in `security/redteam-
findings.yaml` must trace to a permanent regression test. This asserts that each
`test:` names a real `def <test>(` under the test roots — so DELETING the regression
test for a known exploit FAILS CI, which is §14.4's traceability requirement. It also
rejects a malformed entry (missing field) so the register cannot silently rot.

Pure filesystem + YAML — no DB, no network. Exit 0 clean, 1 on any violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REGISTER = REPO_ROOT / "security" / "redteam-findings.yaml"
_TEST_ROOTS = ["apps/api/tests", "apps/web/tests"]
_REQUIRED = ("id", "source", "severity", "summary", "test")
_PY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _defined_tests(roots: list[Path]) -> set[str]:
    """Every `def test_...(` (py) / `test(`/`it(` (ts) name under the roots."""
    names: set[str] = set()
    py_def = re.compile(r"^\s*(?:async\s+)?def\s+(test\w+)\s*\(", re.MULTILINE)
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".py", ".ts", ".tsx"} or not path.is_file():
                continue
            names.update(py_def.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return names


def collect_errors(doc: dict[str, Any], repo_root: Path) -> list[str]:
    entries = doc.get("findings")
    if not isinstance(entries, list):
        return ["findings-register: top-level `findings:` must be a list"]
    defined = _defined_tests([repo_root / r for r in _TEST_ROOTS])
    errors: list[str] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(entries):
        tag = entry.get("id", f"findings[{i}]") if isinstance(entry, dict) else f"findings[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{tag}: not a mapping")
            continue
        for field in _REQUIRED:
            if not entry.get(field):
                errors.append(f"{tag}: missing required field '{field}'")
        fid = entry.get("id")
        if fid in seen_ids:
            errors.append(f"{tag}: duplicate finding id")
        elif fid:
            seen_ids.add(fid)
        test = entry.get("test")
        if test:
            # Must be a plain test-function identifier AND resolve to a real def. A
            # value with spaces / '::' / typos previously slipped through the
            # identifier guard and silently "passed" — now it fails (review #5).
            if not _PY_NAME.match(str(test)):
                errors.append(f"{tag}: test '{test}' is not a bare test-function identifier")
            elif str(test) not in defined:
                errors.append(
                    f"{tag}: regression test '{test}' is not defined under {_TEST_ROOTS} — "
                    "the test for a known exploit was removed or renamed (TITAN-SEC-131)"
                )
    return errors


def main() -> None:
    if not REGISTER.exists():
        raise SystemExit(f"findings-register: missing {REGISTER}")
    doc = yaml.safe_load(REGISTER.read_text(encoding="utf-8")) or {}
    errors = collect_errors(doc, REPO_ROOT)
    if errors:
        sys.stderr.write("findings-register check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)
    n = len(doc.get("findings") or [])
    sys.stdout.write(f"findings-register OK: {n} finding(s), every regression test resolves.\n")


if __name__ == "__main__":
    main()
