"""The §6.6 Code-Review rejection table as typed rules (TITAN-WFL-032).

R1-R11 are encoded as data, not prompt text, so the verdict is computed
deterministically and the same taxonomy is cited by every finding, persisted to
the Phase-44 ``loop_findings`` ledger, and asserted by the regression fixtures.
The reviewer AGENT still authors findings; this module fixes what a finding may
cite and how a set of findings aggregates into a verdict.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# Severity → convergence/verdict weight. Mirrors titan.loop.engine.SEVERITY_ORDER;
# the reviewer taxonomy only ever assigns blocking or major (R1-R11), but findings
# flowing through the loop may also be minor/info.
BLOCKING = "blocking"
MAJOR = "major"


class ReviewRule(BaseModel):
    """One row of the §6.6 rejection table."""

    model_config = {"frozen": True}

    code: str = Field(pattern=r"^R(?:[1-9]|1[01])$")
    title: str
    severity: Literal["blocking", "major"]
    summary: str


class Verdict(StrEnum):
    """The reviewer's authority (§6.6): approve, approve-with-debt, or reject."""

    APPROVED = "APPROVED"
    APPROVED_WITH_COMMENTS = "APPROVED_WITH_COMMENTS"
    REJECTED = "REJECTED"


# The rejection table verbatim (§6.6). Order is R1..R11.
RUBRIC: dict[str, ReviewRule] = {
    rule.code: rule
    for rule in (
        ReviewRule(
            code="R1",
            title="Clean Architecture / SOLID violation",
            severity="blocking",
            summary="Violates Clean Architecture boundaries (§6.1) or SOLID at module level",
        ),
        ReviewRule(
            code="R2",
            title="Missing/failing tests or diff coverage below gates",
            severity="blocking",
            summary="Missing or failing tests; diff coverage below the §6.5 gates",
        ),
        ReviewRule(
            code="R3",
            title="Security finding / hardcoded secret / missing input validation",
            severity="blocking",
            summary="High/critical security finding, hardcoded secret, or missing input "
            "validation on external input",
        ),
        ReviewRule(
            code="R4",
            title="Duplicated logic where an abstraction applies",
            severity="blocking",
            summary="Duplicated logic (>10 similar lines) where an existing abstraction applies",
        ),
        ReviewRule(
            code="R5",
            title="Swallowed errors / bare excepts / client-visible stack traces",
            severity="blocking",
            summary="Swallowed errors, bare excepts, or client-visible stack traces (§6.3)",
        ),
        ReviewRule(
            code="R6",
            title="Unjustified pattern use (CQRS/event-sourcing without ADR)",
            severity="blocking",
            summary="CQRS or event-sourcing introduced without a linked ADR (§6.1)",
        ),
        ReviewRule(
            code="R7",
            title="Placeholder code",
            severity="blocking",
            summary="TODO/FIXME, stubbed function bodies, or commented-out code",
        ),
        ReviewRule(
            code="R8",
            title="Public API change without OpenAPI update + versioning",
            severity="blocking",
            summary="Public API change without an OpenAPI update + versioning per the "
            "ArchitecturePackage",
        ),
        ReviewRule(
            code="R9",
            title="Performance budget exceeded or N+1 introduced",
            severity="blocking",
            summary="Performance budget exceeded or an N+1 query introduced",
        ),
        ReviewRule(
            code="R10",
            title="Naming/lint violations that bypassed CI",
            severity="major",
            summary="Naming or lint violations that somehow bypassed CI",
        ),
        ReviewRule(
            code="R11",
            title="Missing docstrings / migration rollback",
            severity="major",
            summary="Missing docstrings/JSDoc on public functions, or a missing migration rollback",
        ),
    )
}

# §6.6: "Two major findings equal one blocking condition."
MAJORS_PER_BLOCKING = 2


def rule(code: str) -> ReviewRule:
    """Look up a rubric rule, raising a clear error on an unknown code."""
    try:
        return RUBRIC[code]
    except KeyError:
        raise KeyError(f"unknown review rule {code!r}; expected one of {sorted(RUBRIC)}") from None


def aggregate_verdict(
    *, blocking_count: int, major_count: int, minor_or_advisory_count: int
) -> Verdict:
    """Compute the §6.6 verdict from BLOCKING findings' counts.

    REJECTED when there is any blocking finding OR two majors make a blocking
    condition. APPROVED_WITH_COMMENTS when only minor/advisory notes remain (they
    become debt tickets, never a block). APPROVED when the diff is clean. Callers
    pass counts of findings that ACTUALLY block — advisory/out-of-blast-radius
    findings (TITAN-WFL-033) are excluded before this point.
    """
    effective_blocking = blocking_count + (major_count // MAJORS_PER_BLOCKING)
    if effective_blocking > 0:
        return Verdict.REJECTED
    if major_count or minor_or_advisory_count:
        return Verdict.APPROVED_WITH_COMMENTS
    return Verdict.APPROVED


__all__ = [
    "BLOCKING",
    "MAJOR",
    "MAJORS_PER_BLOCKING",
    "RUBRIC",
    "ReviewRule",
    "Verdict",
    "aggregate_verdict",
    "rule",
]
