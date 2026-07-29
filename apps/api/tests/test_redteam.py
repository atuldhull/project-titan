"""Phase 75 (Vol 8 §14.4): the LLM red-team harness + findings register (SEC-130/131).

DoD: the corpus run against the REAL controls reports zero unauthorized tool
executions and zero secret-bearing payloads. Re-broken: the harness FLAGS a control
that wrongly allows an out-of-allowlist tool, and one that fails to redact a secret;
and the findings register requires every finding to trace to a live regression test.
Pure — no DB/network.
"""

from __future__ import annotations

from typing import Any

import yaml
from scripts.check_redteam_findings import REGISTER, REPO_ROOT, collect_errors

from titan.redaction import default_engine
from titan.redteam import CORPUS, RedTeamHarness
from titan.tools.allowlists import allowed_for_role

pytestmark: list[Any] = []


def _real_harness() -> RedTeamHarness:
    engine = default_engine()
    return RedTeamHarness(
        tool_authorized=allowed_for_role,
        redact=lambda text: engine.redact(text, sink="redteam"),
    )


def test_corpus_passes_against_the_real_controls() -> None:
    report = _real_harness().run()
    assert report.passed, report.to_dict()
    assert report.total == len(CORPUS) >= 12
    assert report.unauthorized_tool_executions == 0
    assert report.secret_bearing_payloads == 0


def test_every_tool_abuse_case_is_actually_denied_by_authz() -> None:
    # A well-formed corpus: each named (role, tool) really is out of the allowlist.
    for case in CORPUS:
        if case.category in ("tool_abuse", "prompt_injection"):
            assert case.role and case.tool
            assert not allowed_for_role(case.role, case.tool), f"{case.id} is NOT denied!"


def test_harness_flags_an_unauthorized_tool_execution() -> None:
    # A control that wrongly ALLOWS everything must be caught.
    harness = RedTeamHarness(tool_authorized=lambda _r, _t: True, redact=lambda t: t)
    report = harness.run()
    assert not report.passed
    assert report.unauthorized_tool_executions >= 6  # all tool-abuse + injection cases


def test_harness_flags_a_secret_that_survives_redaction() -> None:
    # A redactor that leaks (identity) must be caught on the secret-exfil cases.
    harness = RedTeamHarness(tool_authorized=lambda _r, _t: False, redact=lambda t: t)
    report = harness.run()
    assert not report.passed
    assert report.secret_bearing_payloads >= 4


def test_findings_register_is_consistent() -> None:
    doc = yaml.safe_load(REGISTER.read_text("utf-8"))
    assert collect_errors(doc, REPO_ROOT) == []


def test_findings_register_flags_a_removed_regression_test() -> None:
    doc = {
        "findings": [
            {
                "id": "F-x",
                "source": "adversarial-review",
                "severity": "high",
                "summary": "s",
                "test": "test_this_regression_was_deleted",
            }
        ]
    }
    errors = collect_errors(doc, REPO_ROOT)
    assert any("test_this_regression_was_deleted" in e for e in errors), errors
