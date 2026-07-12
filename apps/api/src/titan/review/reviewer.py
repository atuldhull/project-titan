"""The deterministic rubric reviewer (TITAN-WFL-032/033).

``review_pr`` runs every R1-R11 detector over a PullRequest, applies the §6.6
scope discipline (a finding whose line was not ADDED by the diff is outside the
blast radius → ``advisory`` and can never block), and aggregates the §6.6
verdict. It is pure and provider-free, so the fixture regression suite proves the
rubric without an LLM. Its findings carry the §6.6 rule code and advisory flag;
when a driver feeds them through the Phase-44 loop those citations persist to
``loop_findings`` (via ``engine.Finding.rule_code``/``advisory``) and their
refute-or-fix disposition is handled by the existing loop service.
"""

from __future__ import annotations

from titan.review.detectors import DETECTORS
from titan.review.models import PullRequest, ReviewFinding, ReviewResult
from titan.review.rubric import aggregate_verdict


def _in_blast_radius(finding: ReviewFinding, added: dict[str, frozenset[int]]) -> bool:
    """A finding is in the blast radius when it sits on a line the diff added.

    When a file lists no added lines (a fixture that did not annotate them), we
    cannot prove it is out of radius, so we keep it in-radius rather than silently
    demote a real blocker to advisory. A file-level finding (line 0) is in-radius.
    """
    lines = added.get(finding.file)
    if not lines or finding.line == 0:
        return True
    return finding.line in lines


def review_pr(pr: PullRequest) -> ReviewResult:
    """Review one PR against the full rubric and return findings + verdict."""
    added = {f.path: f.added_lines for f in pr.files}

    findings: list[ReviewFinding] = []
    for detector in DETECTORS.values():
        for finding in detector(pr):
            findings.append(
                finding.model_copy(update={"advisory": not _in_blast_radius(finding, added)})
            )

    blocking = sum(1 for f in findings if not f.advisory and f.severity == "blocking")
    major = sum(1 for f in findings if not f.advisory and f.severity == "major")
    other = sum(1 for f in findings if f.advisory or f.severity not in ("blocking", "major"))

    verdict = aggregate_verdict(
        blocking_count=blocking, major_count=major, minor_or_advisory_count=other
    )
    # Stable order: blocking first, then by file/line — deterministic reports.
    findings.sort(key=lambda f: (f.advisory, f.severity != "blocking", f.file, f.line))
    return ReviewResult(verdict=verdict, findings=tuple(findings))


__all__ = ["review_pr"]
