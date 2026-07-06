"""code.* tests (Phase 21; Vol 7 §5).

The JUnit/coverage parsers are pure and unit-tested WITHOUT Docker; the
run_python/run_node/test executors are exercised against the real sandbox
(``sandbox`` marker).
"""

from collections.abc import Iterator

import pytest

from titan.tools.builtin import code
from titan.tools.errors import ToolError

from ._sandbox_tools import ToolHarness, docker_ready
from .conftest import run_async

# --- pure parser units (no Docker) -------------------------------------------

_JUNIT = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="s" tests="4" failures="1" errors="1" skipped="1">
    <testcase classname="pkg.mod" name="test_ok"/>
    <testcase classname="pkg.mod" name="test_bad">
      <failure message="assert failed">tb</failure>
    </testcase>
    <testcase classname="pkg.mod" name="test_boom"><error message="boom"/></testcase>
    <testcase classname="pkg.mod" name="test_skip"><skipped/></testcase>
  </testsuite>
</testsuites>"""


def test_parse_junit_totals_and_failures() -> None:
    summary = code.parse_junit(_JUNIT)
    assert summary["total"] == 4
    assert summary["passed"] == 1
    assert summary["failed"] == 1 and summary["errors"] == 1 and summary["skipped"] == 1
    kinds = {f["type"] for f in summary["failures"]}
    assert kinds == {"failure", "error"}
    assert summary["failures"][0]["name"] == "pkg.mod::test_bad"


def test_parse_junit_bare_testsuite() -> None:
    summary = code.parse_junit('<testsuite tests="2" failures="0" errors="0" skipped="0"/>')
    assert summary["total"] == 2 and summary["passed"] == 2


def test_parse_junit_malformed_raises() -> None:
    with pytest.raises(ToolError) as exc:
        code.parse_junit("<not valid xml")
    assert exc.value.code == "UPSTREAM_ERROR"


def test_parse_coverage_line_rate() -> None:
    assert code.parse_coverage('<coverage line-rate="0.8734"/>') == 87.34
    assert code.parse_coverage("<coverage/>") is None
    assert code.parse_coverage("garbage") is None


# --- executors against the real sandbox --------------------------------------

_sandbox = pytest.mark.sandbox


@pytest.fixture
def harness() -> Iterator[ToolHarness]:
    if not docker_ready():
        pytest.skip("no Docker / titan-sandbox:local image for the code suite")
    h = ToolHarness()
    try:
        yield h
    finally:
        h.close()


@_sandbox
def test_run_python_inline(harness: ToolHarness) -> None:
    result = run_async(lambda: code._run_python({"code": "print(6*7)"}, harness.ctx())).result
    assert result["passed"] is True and result["stdout"].strip() == "42"


@_sandbox
def test_run_python_requires_exactly_one_source(harness: ToolHarness) -> None:
    with pytest.raises(ToolError) as exc:
        run_async(lambda: code._run_python({"code": "x", "file": "y.py"}, harness.ctx()))
    assert exc.value.code == "SCHEMA_INVALID"


@_sandbox
def test_run_node_inline(harness: ToolHarness) -> None:
    result = run_async(lambda: code._run_node({"code": "console.log(2+3)"}, harness.ctx())).result
    assert result["passed"] is True and result["stdout"].strip() == "5"


@_sandbox
def test_code_test_parses_reports_from_fixture(harness: ToolHarness) -> None:
    # A fixture repo: a passing test suite plus committed JUnit + coverage reports.
    (harness.workspace.root / "junit.xml").write_text(
        '<testsuite tests="3" failures="1" errors="0" skipped="0">'
        '<testcase name="a"/><testcase name="b"><failure message="x"/></testcase>'
        '<testcase name="c"/></testsuite>'
    )
    (harness.workspace.root / "coverage.xml").write_text('<coverage line-rate="0.9"/>')
    result = run_async(lambda: code._test({"command": ["true"]}, harness.ctx())).result
    assert result["junit"]["total"] == 3 and result["junit"]["failed"] == 1
    assert result["coverage_percent"] == 90.0
