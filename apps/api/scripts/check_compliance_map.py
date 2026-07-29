"""CI consistency check for the OWASP compliance map (TITAN-SEC-110, Vol 8 §12).

`security/compliance-map.yaml` traces every OWASP Top 10 (2021) and OWASP LLM Top 10
(2025) category to the TITAN requirement IDs that address it and to the test tokens
(`TITAN-SEC-NNN`) that prove them. This script enforces that mapping so the document
cannot silently drift from the suite:

  1. every `TITAN-SEC-NNN` token listed under a row's `tests:` resolves to at least
     one test file under `meta.test_roots` — so DELETING A MAPPED TEST FAILS CI,
     which is exactly §12's stated acceptance criterion;
  2. every row is accounted for — it must carry a non-empty `tests:` list OR an
     explicit `coverage:` marker (design/config/process/ci). A row that is silently
     both testless and unmarked is an untracked coverage gap and fails the check;
  3. both taxonomies are complete: A01..A10 and LLM01..LLM10 are all present.

Pure filesystem + YAML — no DB, no network. Exit 0 clean, 1 on any violation.

Usage: uv run python scripts/check_compliance_map.py
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
MAP_PATH = REPO_ROOT / "security" / "compliance-map.yaml"

_TOKEN_RE = re.compile(r"TITAN-SEC-\d+")
_TEST_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}
_VALID_COVERAGE = {"design", "config", "process", "ci"}
_EXPECTED_A = {f"A{n:02d}" for n in range(1, 11)}
_EXPECTED_LLM = {f"LLM{n:02d}" for n in range(1, 11)}
# The ONLY rows permitted to satisfy the map with a `coverage:` marker instead of
# real tests — the genuinely design/process controls from Vol 8 §12. Any OTHER row
# downgraded from `tests:` to `coverage:` (silently dropping its proving test) is a
# violation, closing the escape-hatch the review flagged (SEC-104 review #5).
_COVERAGE_ONLY_ROWS = frozenset({"A04", "A05", "LLM04", "LLM08", "LLM09"})

_JS_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


def _tokens_in_code(path: Path, text: str) -> set[str]:
    """Tokens that appear in a real STRING literal (not a `#`/`//` comment). A token
    is meaningful only if it rides in source the interpreter parses — an assert
    message, a docstring, a test-id constant — never a comment a maintainer can
    inject for free (SEC-104 review #4; this also un-enshrines the old self-test
    that parked the token in a `# comment`). NOTE: a module/function docstring is a
    string literal and still counts, so deleting/renaming a test module fails the
    gate (§12's acceptance) while a body-gutting that keeps the docstring is not
    caught — an accepted residual documented in security/compliance-map.yaml."""
    if path.suffix.lower() == ".py":
        found: set[str] = set()
        # STRING + FSTRING_MIDDLE (py3.12 f-string literal parts) — both are real
        # string literals; COMMENT tokens are a distinct type and never counted.
        string_types = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type in string_types:
                    found.update(_TOKEN_RE.findall(tok.string))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            # Unparseable file: fall back to a #-comment strip (best effort).
            return set(_TOKEN_RE.findall(re.sub(r"#[^\n]*", "", text)))
        return found
    # Non-Python (TS/JS): strip // and /* */ comments, then search what remains.
    return set(_TOKEN_RE.findall(_JS_COMMENTS.sub("", text)))


def _scan_tokens(roots: list[Path]) -> set[str]:
    """Every `TITAN-SEC-NNN` token that appears in EXECUTABLE code (not comments or
    docstrings) in any test file under the roots."""
    found: set[str] = set()
    for root in roots:
        if not root.exists():
            raise SystemExit(f"compliance-map: test_root does not exist: {root}")
        for path in root.rglob("*"):
            if path.suffix.lower() not in _TEST_SUFFIXES or not path.is_file():
                continue
            found |= _tokens_in_code(path, path.read_text(encoding="utf-8", errors="ignore"))
    return found


def _iter_rows(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for section in ("owasp_top_10_2021", "owasp_llm_top_10_2025"):
        for row in doc.get(section, []) or []:
            rows.append((section, row))
    return rows


def collect_errors(doc: dict[str, Any], repo_root: Path) -> list[str]:
    """The pure consistency check: returns a list of violations (empty == clean)."""
    roots = [repo_root / r for r in doc.get("meta", {}).get("test_roots", [])]
    if not roots:
        return ["compliance-map: meta.test_roots is empty"]

    present = _scan_tokens(roots)
    errors: list[str] = []
    mapped_ids: set[str] = set()

    for _section, row in _iter_rows(doc):
        rid = row.get("id", "<no-id>")
        mapped_ids.add(rid)
        tests = row.get("tests") or []
        coverage = row.get("coverage")

        # (2) every row must be accounted for.
        if not tests and coverage is None:
            errors.append(f"{rid}: no tests and no `coverage:` marker — untracked coverage gap")
        if coverage is not None and coverage not in _VALID_COVERAGE:
            errors.append(f"{rid}: coverage '{coverage}' is not one of {sorted(_VALID_COVERAGE)}")
        # (2b) the coverage escape hatch is closed to the code-backed rows: a row not
        # on the design/process allowlist must NOT drop its proving tests for a marker.
        if coverage is not None and rid not in _COVERAGE_ONLY_ROWS:
            errors.append(
                f"{rid}: uses `coverage: {coverage}` but is not a sanctioned design/process "
                f"row ({sorted(_COVERAGE_ONLY_ROWS)}) — it must be proven by `tests:`"
            )

        # (1) every listed token must resolve, and be a well-formed token.
        for token in tests:
            if not _TOKEN_RE.fullmatch(token):
                errors.append(f"{rid}: '{token}' is not a well-formed TITAN-SEC-NNN token")
            elif token not in present:
                errors.append(
                    f"{rid}: mapped test token {token} resolves to NO test under "
                    f"{[str(r.relative_to(repo_root)) for r in roots]} — "
                    "the test was removed or renamed (TITAN-SEC-110)"
                )

    # (3) taxonomy completeness.
    for missing in sorted(_EXPECTED_A - mapped_ids):
        errors.append(f"OWASP Top 10 2021 row {missing} is missing from the map")
    for missing in sorted(_EXPECTED_LLM - mapped_ids):
        errors.append(f"OWASP LLM Top 10 2025 row {missing} is missing from the map")

    return errors


def main() -> None:
    if not MAP_PATH.exists():
        raise SystemExit(f"compliance-map: missing {MAP_PATH}")
    doc = yaml.safe_load(MAP_PATH.read_text(encoding="utf-8"))
    errors = collect_errors(doc, REPO_ROOT)
    if errors:
        sys.stderr.write("compliance-map consistency check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)

    total_tokens = sum(len(row.get("tests") or []) for _, row in _iter_rows(doc))
    sys.stdout.write(
        f"compliance-map OK: {len(_iter_rows(doc))} rows, "
        f"{total_tokens} mapped test tokens all resolve.\n"
    )


if __name__ == "__main__":
    main()
