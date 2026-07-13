"""Milestone tracking (Vol 6 §4.2, TITAN-WFL-018). PURE — the state machine over
the M0–M4 release train and the regression detector are side-effect-free; the
persistence + pipeline-reopen wiring lives in ``service``.

Milestones are **strictly ordered**: the current milestone always equals the
*highest contiguous* milestone (from M0 up) whose definition of done holds. So a
regression — a new P0 defect after M2, say — makes M2's DoD stop holding, which
drops the current milestone back to M1 and flags the pipeline stage to reopen.

| Milestone | Definition of done | Gate owner |
|-----------|--------------------|------------|
| M0 Inception       | ProjectBrief + ProductBacklog accepted (G2, G3) | CEO |
| M1 Walking Skeleton| Sprint-1 increment deployed to staging; e2e hello path green | DevOps |
| M2 Feature Complete| All MUST-stories DONE; G6–G7 passed; no open P0 | QA |
| M3 Hardened        | G8–G9 passed; perf budgets met; debt reviewed | Security + Reviewer |
| M4 Released        | G10–G12 passed; docs live; user notified | CEO |
"""

from dataclasses import dataclass

#: Ordered milestone ids. ``NONE`` is the sentinel for "below M0".
MILESTONES: tuple[str, ...] = ("M0", "M1", "M2", "M3", "M4")
NONE: str = "none"

#: When the project regresses INTO a milestone band (its DoD newly fails), the
#: pipeline stage that must reopen to regain the lost milestone. Keyed by the
#: lowest milestone whose DoD now fails.
REOPEN_STAGE_FOR_FAILED: dict[str, str] = {
    "M0": "ceo_vision",
    "M1": "devops_deploy",
    "M2": "engineering",
    "M3": "security",
    "M4": "ceo_release",
}


@dataclass(frozen=True)
class MilestoneSignals:
    """The machine-checkable facts the milestone DoDs read. Computed from the gate
    ledger, task states, and the debt report by the caller; passed in explicitly so
    the evaluator stays pure and fixture-testable."""

    brief_accepted: bool = False  # G2
    backlog_accepted: bool = False  # G3
    skeleton_deployed: bool = False  # sprint-1 increment on staging
    hello_path_green: bool = False  # e2e hello path
    must_stories_done: bool = False
    g6_g7_passed: bool = False
    open_p0_count: int = 0
    g8_g9_passed: bool = False
    perf_budgets_met: bool = False
    debt_reviewed: bool = False
    g10_g12_passed: bool = False
    docs_live: bool = False
    user_notified: bool = False


@dataclass(frozen=True)
class MilestoneOutcome:
    """The evaluated milestone state and, on a regression, what to reopen."""

    current: str  # NONE or one of MILESTONES
    previous: str
    regressed: bool
    #: The lowest milestone whose DoD newly fails (drives the reopen), else None.
    failed_milestone: str | None
    #: The pipeline stage to reopen on a regression, else None.
    reopen_stage: str | None


def _dod_holds(milestone: str, s: MilestoneSignals) -> bool:
    if milestone == "M0":
        return s.brief_accepted and s.backlog_accepted
    if milestone == "M1":
        return s.skeleton_deployed and s.hello_path_green
    if milestone == "M2":
        return s.must_stories_done and s.g6_g7_passed and s.open_p0_count == 0
    if milestone == "M3":
        return s.g8_g9_passed and s.perf_budgets_met and s.debt_reviewed
    if milestone == "M4":
        return s.g10_g12_passed and s.docs_live and s.user_notified
    raise ValueError(f"unknown milestone {milestone!r}")


def current_milestone(signals: MilestoneSignals) -> str:
    """The highest *contiguous* milestone (from M0) whose DoD holds. Strict
    ordering: M2 cannot be current if M1's DoD does not hold."""
    reached = NONE
    for milestone in MILESTONES:
        if _dod_holds(milestone, signals):
            reached = milestone
        else:
            break
    return reached


def _index(milestone: str) -> int:
    """Ordinal for comparison; NONE sorts below M0."""
    return -1 if milestone == NONE else MILESTONES.index(milestone)


def evaluate(previous: str, signals: MilestoneSignals) -> MilestoneOutcome:
    """Compute the new milestone from ``signals`` and compare it to ``previous``. A
    drop is a regression: the lowest newly-failing milestone selects the pipeline
    stage to reopen (TITAN-WFL-018)."""
    current = current_milestone(signals)
    regressed = _index(current) < _index(previous)
    failed_milestone: str | None = None
    reopen_stage: str | None = None
    if regressed:
        # The lowest milestone that now fails is the one just above `current`.
        failed_idx = _index(current) + 1
        failed_milestone = MILESTONES[failed_idx]
        reopen_stage = REOPEN_STAGE_FOR_FAILED[failed_milestone]
    return MilestoneOutcome(
        current=current,
        previous=previous,
        regressed=regressed,
        failed_milestone=failed_milestone,
        reopen_stage=reopen_stage,
    )


__all__ = [
    "MILESTONES",
    "NONE",
    "REOPEN_STAGE_FOR_FAILED",
    "MilestoneOutcome",
    "MilestoneSignals",
    "current_milestone",
    "evaluate",
]
