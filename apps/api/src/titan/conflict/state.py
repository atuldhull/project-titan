"""The conflict-resolution state machine — pure, no I/O (Vol 2 §7, TITAN-AGT-017/020).

§7's escalation ladder is encoded here as data, so the flow is enforced by code and
never by prompt compliance. A dispute moves:

    direct ──no consensus──▶ arbitration ──arbiter verdict──▶ resolved
      │                          │
      │ cross-domain             │ arbitration disputed / cross-domain
      ▼                          ▼
    executive ◀─────────────────┘── CEO decides ──▶ resolved

Rule 2 appoints the arbiter *by the dispute's kind* — Architect for technical, PM
Product for scope, PM Project for priority/assignment (§7.2). That mapping is the
mechanically-checkable core of TITAN-AGT-017: given the kind, the arbiter is fixed,
never chosen by the disputing parties. Rule 5 (deadlock) escalates one level when a
conflict reaches ``max_conflict_rounds`` or a task is reopened ``max_task_reopens``
times; thresholds are injected (they live in the DB, TITAN-AGT-020).
"""

from dataclasses import dataclass
from typing import Literal

# The four persisted states (§7 rules 1–3). ``resolved`` is terminal.
ConflictState = Literal["direct", "arbitration", "executive", "resolved"]

# How a dispute is classified — decides which arbiter rule 2 appoints.
DisputeKind = Literal["technical", "scope", "priority", "cross_domain"]

# Why a transition fired (the audit trail's ``trigger`` column).
TransitionTrigger = Literal[
    "opened",
    "consensus",
    "no_consensus",
    "cross_domain",
    "arbiter_verdict",
    "disputed",
    "executive_decision",
    "deadlock_escalation",
]

# §7.2 arbiter appointment: kind → arbiter role. cross_domain has no arbiter — it
# goes straight to the CEO (executive), per §7.3.
ARBITER_FOR_KIND: dict[str, str | None] = {
    "technical": "architect",
    "scope": "pm_product",
    "priority": "pm_project",
    "cross_domain": None,
}

# The legal state graph. A move not in this set is a programming/authority error,
# never silently applied — the service raises on it (TITAN-AGT-017: the runtime,
# not a prompt, decides what is reachable).
ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # rule 1: a direct round either reaches consensus or fails to.
        ("direct", "resolved"),  # consensus — "most disputes end here"
        ("direct", "arbitration"),  # no consensus after round 1 → rule 2
        ("direct", "executive"),  # a cross-domain dispute skips arbitration → rule 3
        # rule 2: the arbiter's verdict binds, unless disputed/cross-domain.
        ("arbitration", "resolved"),  # binding arbiter verdict
        ("arbitration", "executive"),  # arbitration disputed / crosses domains → rule 3
        # rule 3: the CEO's decision is final inside the platform.
        ("executive", "resolved"),
    }
)

# The level each state escalates to under the deadlock rule (§7.5). ``executive`` is
# already the top platform level, so a deadlock there stays executive but is flagged
# for the human dashboard (rule 6 tripwire territory) rather than moving.
_ESCALATION_LADDER: dict[str, ConflictState] = {
    "direct": "arbitration",
    "arbitration": "executive",
    "executive": "executive",
}

DEFAULT_MAX_CONFLICT_ROUNDS = 3
DEFAULT_MAX_TASK_REOPENS = 3


@dataclass(frozen=True)
class DeadlockThresholds:
    """Rule-5 thresholds. Injected from the DB so a change needs no redeploy."""

    max_conflict_rounds: int = DEFAULT_MAX_CONFLICT_ROUNDS
    max_task_reopens: int = DEFAULT_MAX_TASK_REOPENS


def arbiter_for(kind: str) -> str | None:
    """The arbiter role §7.2 appoints for ``kind`` (None for a cross-domain
    dispute, which escalates straight to the CEO). Raises on an unknown kind so a
    mis-classified dispute fails loudly rather than routing to nobody."""
    if kind not in ARBITER_FOR_KIND:
        raise ValueError(f"unknown dispute kind '{kind}'")
    return ARBITER_FOR_KIND[kind]


def is_legal_transition(from_state: str, to_state: str) -> bool:
    """Whether ``from_state → to_state`` is a move the §7 machine permits."""
    return (from_state, to_state) in ALLOWED_TRANSITIONS


def is_terminal(state: str) -> bool:
    return state == "resolved"


def escalate_one_level(state: str) -> ConflictState:
    """The state a deadlock (rule 5) escalates ``state`` to (§7.5). At the top
    (``executive``) there is nowhere higher inside the platform, so it stays put
    and is flagged for the human instead."""
    if state not in _ESCALATION_LADDER:
        raise ValueError(f"cannot escalate from terminal/unknown state '{state}'")
    return _ESCALATION_LADDER[state]


def round_deadlocked(round_no: int, thresholds: DeadlockThresholds) -> bool:
    """Rule 5: a conflict that reaches ``max_conflict_rounds`` without resolving."""
    return round_no >= thresholds.max_conflict_rounds


def reopens_deadlocked(reopen_count: int, thresholds: DeadlockThresholds) -> bool:
    """Rule 5: a task reopened ``max_task_reopens`` times auto-escalates."""
    return reopen_count >= thresholds.max_task_reopens


__all__ = [
    "ALLOWED_TRANSITIONS",
    "ARBITER_FOR_KIND",
    "DEFAULT_MAX_CONFLICT_ROUNDS",
    "DEFAULT_MAX_TASK_REOPENS",
    "ConflictState",
    "DeadlockThresholds",
    "DisputeKind",
    "TransitionTrigger",
    "arbiter_for",
    "escalate_one_level",
    "is_legal_transition",
    "is_terminal",
    "reopens_deadlocked",
    "round_deadlocked",
]
