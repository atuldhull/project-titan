"""The fixed 10-brief benchmark suite schema + loader (Phase 55). Pure — no DB."""

import pytest
from pydantic import ValidationError

from titan.benchmark.suite import SUITE_SIZE, BenchmarkSuite, Brief, load_suite


def _brief(bid: str, *, executable: bool = False, local_only: bool = False) -> Brief:
    return Brief(
        id=bid, title=bid, prompt="do a thing", executable=executable, local_only=local_only
    )


def test_packaged_suite_has_the_fixed_shape() -> None:
    suite = load_suite()
    assert len(suite.briefs) == SUITE_SIZE
    assert suite.executable_brief.id == "todo-tiny"
    assert suite.executable_brief.executable
    # §7 local-only viability numerator is real (MVP wants >= 3/10).
    assert suite.local_only_count >= 3
    assert len({b.id for b in suite.briefs}) == SUITE_SIZE


def test_executable_brief_is_the_task_tracking_app() -> None:
    """The one executable brief is the §9.1 reference scenario (Phase 56 reuses it)."""
    brief = load_suite().executable_brief
    assert "task" in brief.prompt.lower()
    assert brief.expected_features  # traceability targets exist


def test_by_id_and_missing() -> None:
    suite = load_suite()
    assert suite.by_id("url-shortener").title
    with pytest.raises(KeyError):
        suite.by_id("does-not-exist")


def test_suite_rejects_wrong_count() -> None:
    with pytest.raises(ValidationError):
        BenchmarkSuite(briefs=[_brief("a", executable=True)])


def test_suite_rejects_zero_or_multiple_executables() -> None:
    nine = [_brief(f"b{i}") for i in range(9)]
    with pytest.raises(ValidationError):
        BenchmarkSuite(briefs=[*nine, _brief("x")])  # zero executable
    two = [_brief(f"c{i}", executable=(i < 2)) for i in range(SUITE_SIZE)]
    with pytest.raises(ValidationError):
        BenchmarkSuite(briefs=two)  # two executable


def test_suite_rejects_duplicate_ids() -> None:
    dupes = [_brief("dup", executable=(i == 0)) for i in range(SUITE_SIZE)]
    with pytest.raises(ValidationError):
        BenchmarkSuite(briefs=dupes)
