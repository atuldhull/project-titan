"""The delivery-pipeline stage machine (Vol 6 §2.1, TITAN-WFL-001/002).

The single source of truth for the ordered 12-stage macro flow: CEO vision → PM
backlog → Project Manager plan → Architect design → Engineering → QA → Security →
Code Review → DevOps deploy → Documentation → CEO release approval → handover.

Pure data + lookup — no DB, no I/O — so the no-skip rule is trivially
unit-testable: the ONLY legal advance from a stage is to its immediate successor
(TITAN-WFL-002). Any other target is an out-of-order transition the service
rejects, leaving state unchanged. The one sanctioned skip is a CEO
``NOT_APPLICABLE`` marking (handled in the service with a logged justification),
which still advances to the immediate successor — it never reorders or jumps.
"""

from dataclasses import dataclass

# The 12 stages of the macro flow (§2.1), in strict order. The key is the
# persisted ``pipeline_states.current_stage`` value; the label is for the
# dashboard / ledger.
STAGES: tuple[str, ...] = (
    "ceo_vision",
    "pm_backlog",
    "pmgr_plan",
    "architect_design",
    "engineering",
    "qa",
    "security",
    "code_review",
    "devops_deploy",
    "documentation",
    "ceo_release",
    "handover",
)

STAGE_LABELS: dict[str, str] = {
    "ceo_vision": "CEO vision & prioritization",
    "pm_backlog": "PM backlog (user stories + AC)",
    "pmgr_plan": "Project Manager plan (tasks, sprints, DAG)",
    "architect_design": "Architect design package",
    "engineering": "Engineering (self-improvement loop)",
    "qa": "QA (integration + e2e + regression)",
    "security": "Security (audit + scans + OWASP)",
    "code_review": "Code review (release-level)",
    "devops_deploy": "DevOps deploy (staging → prod)",
    "documentation": "Documentation bundle",
    "ceo_release": "CEO release approval",
    "handover": "Release & handover",
}

#: The first stage every pipeline enters.
INITIAL_STAGE: str = STAGES[0]

#: The terminal stage: reaching it (releasing) ends the pipeline.
TERMINAL_STAGE: str = STAGES[-1]

_INDEX: dict[str, int] = {stage: i for i, stage in enumerate(STAGES)}


def is_stage(stage: str) -> bool:
    """Whether ``stage`` is one of the 12 canonical stages."""
    return stage in _INDEX


def stage_index(stage: str) -> int:
    """The 0-based ordinal of ``stage``; raises KeyError for an unknown stage."""
    return _INDEX[stage]


def next_stage(stage: str) -> str | None:
    """The immediate successor of ``stage``, or None if it is terminal."""
    i = _INDEX[stage]
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


def is_terminal(stage: str) -> bool:
    """Whether ``stage`` is the final stage (no successor to advance to)."""
    return stage == TERMINAL_STAGE


@dataclass(frozen=True)
class StageAdvance:
    """A resolved legal advance: ``from_stage`` → ``to_stage`` (its successor)."""

    from_stage: str
    to_stage: str


def resolve_advance(current: str, target: str) -> StageAdvance | None:
    """The legal advance from ``current`` to ``target``, or None if illegal.

    Legal iff ``target`` is exactly the immediate successor of ``current``
    (TITAN-WFL-002 — no skipping, no reordering, no self-loops). A target that is
    the same stage, an earlier stage, or a later-but-not-adjacent stage all
    resolve to None → the service raises a rejected-transition event.
    """
    if current not in _INDEX or target not in _INDEX:
        return None
    if next_stage(current) != target:
        return None
    return StageAdvance(from_stage=current, to_stage=target)


__all__ = [
    "INITIAL_STAGE",
    "STAGES",
    "STAGE_LABELS",
    "TERMINAL_STAGE",
    "StageAdvance",
    "is_stage",
    "is_terminal",
    "next_stage",
    "resolve_advance",
    "stage_index",
]
