"""The Code Reviewer rubric (Vol 6 §6.6, TITAN-WFL-032/033; Phase 47).

Encodes the §6.6 rejection table R1-R11 as typed rules, a deterministic detector
per rule, and the verdict aggregation (two majors == one blocking). ``review_pr``
runs the rubric over a :class:`PullRequest` and returns cited findings + a
verdict, so the rubric is regression-testable without a provider. Findings carry
the §6.6 rule code and an advisory flag; when they flow through the Phase-44 loop
those citations are persisted to ``loop_findings`` (TITAN-WFL-033) and their
refute-or-fix disposition is handled by the existing loop service (no second
review pipeline, no second disposition store).
"""

from titan.review.models import ChangedFile, PullRequest, ReviewFinding, ReviewResult
from titan.review.reviewer import review_pr
from titan.review.rubric import RUBRIC, ReviewRule, Verdict, aggregate_verdict

__all__ = [
    "RUBRIC",
    "ChangedFile",
    "PullRequest",
    "ReviewFinding",
    "ReviewResult",
    "ReviewRule",
    "Verdict",
    "aggregate_verdict",
    "review_pr",
]
