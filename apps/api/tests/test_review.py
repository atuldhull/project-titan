"""The §6.6 Code-Review rubric, its detectors, and the regression fixtures
(Phase 47, TITAN-WFL-032/033). Pure and hermetic — no provider, no DB."""

import pytest

from tests.review_fixtures import CLEAN_FIXTURES, FIXTURES, RULE_FIXTURES, Fixture
from titan.review.models import ChangedFile, PullRequest, ReviewFinding
from titan.review.reviewer import review_pr
from titan.review.rubric import RUBRIC, Verdict, aggregate_verdict, rule

# --- the rubric itself -------------------------------------------------------


def test_rubric_has_all_eleven_rules_with_the_spec_severities() -> None:
    assert set(RUBRIC) == {f"R{i}" for i in range(1, 12)}
    # §6.6: R1-R9 blocking, R10-R11 major.
    for i in range(1, 10):
        assert RUBRIC[f"R{i}"].severity == "blocking"
    assert RUBRIC["R10"].severity == "major"
    assert RUBRIC["R11"].severity == "major"


def test_unknown_rule_code_is_rejected() -> None:
    with pytest.raises(KeyError):
        rule("R12")


def test_verdict_aggregation_follows_two_majors_equal_a_blocking() -> None:
    assert aggregate_verdict(blocking_count=0, major_count=0, minor_or_advisory_count=0) == (
        Verdict.APPROVED
    )
    assert aggregate_verdict(blocking_count=0, major_count=1, minor_or_advisory_count=0) == (
        Verdict.APPROVED_WITH_COMMENTS
    )
    # Two majors make one blocking condition -> REJECTED.
    assert aggregate_verdict(blocking_count=0, major_count=2, minor_or_advisory_count=0) == (
        Verdict.REJECTED
    )
    assert aggregate_verdict(blocking_count=1, major_count=0, minor_or_advisory_count=0) == (
        Verdict.REJECTED
    )
    # Advisory/minor notes alone never reject.
    assert aggregate_verdict(blocking_count=0, major_count=0, minor_or_advisory_count=5) == (
        Verdict.APPROVED_WITH_COMMENTS
    )


# --- the regression fixture suite (the phase's core DoD) ---------------------


@pytest.mark.parametrize("fixture", RULE_FIXTURES, ids=lambda f: f.name)
def test_each_rule_fixture_is_rejected_with_its_code(fixture: Fixture) -> None:
    result = review_pr(fixture.pr)
    assert result.verdict == Verdict.REJECTED, f"{fixture.name} should be REJECTED"
    assert fixture.expected_rule in result.rule_codes()
    # The fixture is otherwise-clean, so it cites EXACTLY its target rule.
    assert result.rule_codes() == {fixture.expected_rule}


@pytest.mark.parametrize("fixture", CLEAN_FIXTURES, ids=lambda f: f.name)
def test_clean_fixtures_are_approved(fixture: Fixture) -> None:
    result = review_pr(fixture.pr)
    assert result.verdict == Verdict.APPROVED, result.findings


def test_every_finding_carries_file_line_rule_and_fix() -> None:
    """TITAN-WFL-033: 100% of findings cite file, line, rule id, and a fix."""
    for fixture in FIXTURES:
        for finding in review_pr(fixture.pr).findings:
            assert finding.file
            assert finding.line >= 0
            assert finding.rule_code in RUBRIC
            assert finding.suggestion
            assert finding.message


def test_all_eleven_rules_have_a_fixture() -> None:
    covered = {f.expected_rule for f in RULE_FIXTURES}
    assert covered == set(RUBRIC)


# --- finding discipline: blast radius / advisory -----------------------------


def test_finding_outside_the_blast_radius_is_advisory_and_never_blocks() -> None:
    """A blocking-severity finding on a line the diff did NOT add is advisory and
    must not reject the PR (TITAN-WFL-033)."""
    # A bare except on line 4, but the diff only added line 6 (an unrelated line).
    pr = PullRequest(
        files=(
            ChangedFile(
                path="src/app/application/legacy.py",
                content=(
                    '"""Legacy."""\n\n\n'
                    "def load():\n"
                    "    try:\n"
                    "        return 1\n"
                    "    except:\n"
                    "        pass\n"
                ),
                added_lines=frozenset({6}),  # the except is at line 7, NOT added
            ),
            ChangedFile(path="tests/test_legacy.py", content="def test_x():\n    assert True\n"),
        ),
    )
    result = review_pr(pr)
    r5 = [f for f in result.findings if f.rule_code == "R5"]
    assert r5 and all(f.advisory for f in r5), "the pre-existing bare except must be advisory"
    assert result.verdict != Verdict.REJECTED, "an advisory finding must not block"


def test_finding_on_an_added_line_is_in_radius_and_blocks() -> None:
    pr = PullRequest(
        files=(
            ChangedFile(
                path="src/app/application/new.py",
                content=(
                    '"""New."""\n\n\ndef load():\n    try:\n        return 1\n'
                    "    except:\n        pass\n"
                ),
                added_lines=frozenset({5, 6, 7, 8}),  # the except at line 7 WAS added
            ),
            ChangedFile(path="tests/test_new.py", content="def test_x():\n    assert True\n"),
        ),
    )
    result = review_pr(pr)
    assert result.verdict == Verdict.REJECTED
    assert any(f.rule_code == "R5" and not f.advisory for f in result.findings)


def test_review_finding_requires_a_known_rule_and_a_fix() -> None:
    with pytest.raises(ValueError, match="unknown rule_code"):
        ReviewFinding(rule_code="RZ", file="x.py", line=1, message="m", suggestion="s")
    with pytest.raises(ValueError):
        ReviewFinding(rule_code="R1", file="x.py", line=1, message="m", suggestion="")


# --- false-positive regressions (from the adversarial review) ----------------


def _one_file_pr(content: str, path: str = "src/shop/application/x.py") -> PullRequest:
    return PullRequest(
        files=(
            ChangedFile(path=path, content=content),
            ChangedFile(
                path="tests/test_x.py",
                content='"""t."""\n\n\ndef test_x() -> None:\n    assert True\n',
            ),
        )
    )


def test_r10_does_not_flag_dunder_methods() -> None:
    """A clean, idiomatic class defining dunders must be APPROVED, not REJECTED
    for '__init__ is not snake_case'."""
    pr = _one_file_pr(
        '"""Money."""\n\n\n'
        "class Money:\n"
        '    """A monetary amount."""\n\n'
        "    def __init__(self, cents: int) -> None:\n"
        "        self.cents = cents\n\n"
        "    def __repr__(self) -> str:\n"
        '        return f"Money({self.cents})"\n\n'
        "    def __eq__(self, other: object) -> bool:\n"
        "        return isinstance(other, Money) and other.cents == self.cents\n"
    )
    result = review_pr(pr)
    assert result.verdict == Verdict.APPROVED, result.findings
    # A genuinely bad method name is still caught.
    bad = _one_file_pr(
        '"""X."""\n\n\nclass C:\n    """c."""\n\n'
        "    def doThing(self) -> None:\n        return None\n"
    )
    assert "R10" in review_pr(bad).rule_codes()


def test_r7_does_not_flag_ordinary_english_comments() -> None:
    """Explanatory comments that merely BEGIN with a keyword are prose, not
    commented-out code."""
    pr = _one_file_pr(
        '"""Svc."""\n\n\n'
        "def run(value: int) -> int:\n"
        '    """Clamp negatives."""\n'
        "    # if the value is negative we clamp it to zero\n"
        "    # for large inputs this is still fast\n"
        "    # return early behaviour is intentional\n"
        "    # import order matters here historically\n"
        "    if value < 0:\n"
        "        return 0\n"
        "    return value\n"
    )
    assert review_pr(pr).verdict == Verdict.APPROVED, review_pr(pr).findings
    # Genuinely commented-out code is still caught.
    commented = _one_file_pr(
        '"""X."""\n\n\ndef f() -> int:\n    """f."""\n    # x = compute_total()\n    return 1\n'
    )
    assert "R7" in review_pr(commented).rule_codes()


def test_r9_does_not_flag_dict_get_in_a_loop() -> None:
    """`.get()`/`.first()`/`.all()` on an in-memory container named db/session is
    not an N+1 — only real query methods are."""
    pr = _one_file_pr(
        '"""Cache."""\n\n\n'
        "def totals(session: dict[str, int], ids: list[str]) -> list[int]:\n"
        '    """Look each id up in an in-memory dict."""\n'
        "    out = []\n"
        "    for oid in ids:\n"
        "        out.append(session.get(oid, 0))\n"
        "    return out\n"
    )
    assert review_pr(pr).verdict == Verdict.APPROVED, review_pr(pr).findings


def test_r1_does_not_flag_third_party_layer_name_collision() -> None:
    """A domain module importing `zope.interface` shares a layer NAME but not the
    project package — not a layering violation."""
    pr = _one_file_pr(
        '"""Entity."""\n\n'
        "from zope.interface import Interface\n\n\n"
        "class Widget(Interface):\n"
        '    """A widget."""\n',
        path="src/shop/domain/widget.py",
    )
    assert "R1" not in review_pr(pr).rule_codes()
    # A genuine first-party domain->infrastructure import IS still flagged.
    bad = _one_file_pr(
        '"""E."""\n\nfrom shop.infrastructure.repo import R\n\n\n'
        'def f() -> object:\n    """f."""\n    return R\n',
        path="src/shop/domain/e.py",
    )
    assert "R1" in review_pr(bad).rule_codes()
