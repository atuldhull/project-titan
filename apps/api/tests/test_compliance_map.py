"""Phase 74 (Vol 8 §12): the TITAN-SEC-110 compliance-map consistency check.

DoD: the checked-in `security/compliance-map.yaml` is internally consistent (every
mapped test token resolves; both taxonomies complete); and — the acceptance
criterion itself — REMOVING a mapped test makes the check FAIL. We prove the bite
directly by pointing the checker at a temp repo. The SEC-104-review hardenings are
re-broken too: a COMMENT-only token does not resolve (#4), and a non-design row may
not escape enforcement with a `coverage:` marker (#5).

Pure: no DB, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from scripts.check_compliance_map import REPO_ROOT, collect_errors

pytestmark: list[Any] = []

_FILLER = "TITAN-SEC-770"  # every filler row is proven by this token
_ROW = "TITAN-SEC-778"  # the row-under-test's token


def _write_carrier(tests_dir: Path, name: str, token: str, *, as_comment: bool = False) -> None:
    """Write a .py test file that carries ``token`` in a real string (or a comment)."""
    line = f"# {token}" if as_comment else f'    assert True, "{token} proves this control"'
    body = f"def test_it() -> None:\n{'    pass  ' + line if as_comment else line}\n"
    (tests_dir / name).write_text(body, encoding="utf-8")


def _base(tests_dir: Path) -> None:
    tests_dir.mkdir(exist_ok=True)
    _write_carrier(tests_dir, "test_fillers.py", _FILLER)


def _full_doc(test_root: str, extra_row: dict[str, Any]) -> dict[str, Any]:
    """A taxonomy-complete doc; every filler row is proven by _FILLER, and A01 is the
    row under test (overridden with ``extra_row``)."""
    rows_a = [{"id": f"A{n:02d}", "tests": [_FILLER]} for n in range(1, 11)]
    rows_llm = [{"id": f"LLM{n:02d}", "tests": [_FILLER]} for n in range(1, 11)]
    rows_a[0] = {"id": "A01", **extra_row}
    return {
        "meta": {"test_roots": [test_root]},
        "owasp_top_10_2021": rows_a,
        "owasp_llm_top_10_2025": rows_llm,
    }


def test_live_map_is_consistent() -> None:
    doc = yaml.safe_load((REPO_ROOT / "security" / "compliance-map.yaml").read_text("utf-8"))
    assert collect_errors(doc, REPO_ROOT) == []


def test_removing_a_mapped_test_fails_the_check(tmp_path: Path) -> None:
    _base(tmp_path / "tests")
    _write_carrier(tmp_path / "tests", "test_row.py", _ROW)
    doc = _full_doc("tests", {"tests": [_ROW]})
    assert collect_errors(doc, tmp_path) == []  # token resolves → clean

    (tmp_path / "tests" / "test_row.py").unlink()  # remove the proving test
    errors = collect_errors(doc, tmp_path)
    assert any(_ROW in e for e in errors), errors


def test_comment_only_token_does_not_resolve(tmp_path: Path) -> None:
    # SEC-104 review #4: a token parked in a `# comment` proves nothing — the test
    # body could be deleted while the comment lingers. It must NOT resolve.
    _base(tmp_path / "tests")
    _write_carrier(tmp_path / "tests", "test_row.py", _ROW, as_comment=True)
    doc = _full_doc("tests", {"tests": [_ROW]})
    errors = collect_errors(doc, tmp_path)
    assert any(_ROW in e for e in errors), errors


def test_coverage_escape_hatch_is_closed_for_code_rows(tmp_path: Path) -> None:
    # SEC-104 review #5: A01 (not a design/process row) may not drop its tests for a
    # `coverage:` marker; A04 (a sanctioned design row) may.
    _base(tmp_path / "tests")
    bad = collect_errors(_full_doc("tests", {"coverage": "design"}), tmp_path)
    assert any("A01" in e and "sanctioned" in e for e in bad), bad

    doc = _full_doc("tests", {"tests": [_FILLER]})  # A01 back on tests
    doc["owasp_top_10_2021"][3] = {"id": "A04", "coverage": "design"}  # A04 may use coverage
    assert collect_errors(doc, tmp_path) == []


def test_a_row_without_tests_or_coverage_is_flagged(tmp_path: Path) -> None:
    _base(tmp_path / "tests")
    errors = collect_errors(_full_doc("tests", {}), tmp_path)
    assert any("A01" in e and "untracked coverage gap" in e for e in errors), errors


def test_incomplete_taxonomy_is_flagged(tmp_path: Path) -> None:
    _base(tmp_path / "tests")
    doc = _full_doc("tests", {"tests": [_FILLER]})
    doc["owasp_llm_top_10_2025"] = doc["owasp_llm_top_10_2025"][:-1]  # drop LLM10
    errors = collect_errors(doc, tmp_path)
    assert any("LLM10" in e for e in errors), errors
