"""Blocker detection + the recovery ladder (Vol 6 §4.3, TITAN-WFL-019). PURE — the
detector reads a task snapshot + an injected ``now`` and returns a reason; the
ladder is a fixed ordering. ``service`` runs the ≤60 s sweep, persists blocks, and
performs each rung's side effect.

A task is flagged ``BLOCKED`` when ANY of:

* its dependency failed;
* it is waiting on an unanswered clarification;
* a loop escalation is pending on it;
* no state change for longer than ``2×`` its estimate (a stall).

Recovery runs strictly in order — dependency re-check → reassignment to a fresh
agent instance with summarized context → decomposition into smaller tasks → human
escalation — advancing one rung per sweep while the task stays blocked.
"""

from dataclasses import dataclass
from datetime import datetime

# --- block reasons (checked in priority order) -------------------------------
BLOCK_DEP_FAILED = "dependency_failed"
BLOCK_CLARIFICATION = "awaiting_clarification"
BLOCK_LOOP_ESCALATION = "loop_escalation"
BLOCK_STALLED = "stalled"

#: Task statuses that can be blocked (active, non-terminal). ``blocked`` itself is
#: excluded — a task already blocked is handled by the recovery ladder, not
#: re-detected.
ACTIVE_STATES: frozenset[str] = frozenset(
    {"ready", "assigned", "in_progress", "in_loop", "reopened", "changes_requested"}
)

# --- the recovery ladder -----------------------------------------------------
RUNG_DEPENDENCY_RECHECK = "dependency_recheck"
RUNG_REASSIGN = "reassign"
RUNG_DECOMPOSE = "decompose"
RUNG_HUMAN = "human_escalation"

#: Strict recovery order (TITAN-WFL-019). Human escalation is terminal.
LADDER: tuple[str, ...] = (
    RUNG_DEPENDENCY_RECHECK,
    RUNG_REASSIGN,
    RUNG_DECOMPOSE,
    RUNG_HUMAN,
)


@dataclass(frozen=True)
class TaskSnapshot:
    """The facts the detector reads about one task (assembled by ``service`` from
    the orchestrator tables + cross-domain reads)."""

    task_id: str
    status: str
    estimate_points: int | None
    #: Last state-change proxy — the task row's ``updated_at``.
    updated_at: datetime
    dependency_failed: bool = False
    awaiting_clarification: bool = False
    loop_escalation: bool = False


def detect_block(
    snap: TaskSnapshot,
    now: datetime,
    *,
    stall_multiplier: float,
    hours_per_point: float,
    default_points: int,
) -> str | None:
    """Return the block reason for a task, or None if it is not blocked. Definitive
    signals (failed dep, clarification, loop escalation) win over a stall so the
    reason names the root cause. ``now`` is injected for a deterministic sweep."""
    if snap.status not in ACTIVE_STATES:
        return None
    if snap.dependency_failed:
        return BLOCK_DEP_FAILED
    if snap.awaiting_clarification:
        return BLOCK_CLARIFICATION
    if snap.loop_escalation:
        return BLOCK_LOOP_ESCALATION
    estimate_hours = (snap.estimate_points or default_points) * hours_per_point
    stalled_after = stall_multiplier * estimate_hours * 3600.0
    if (now - snap.updated_at).total_seconds() > stalled_after:
        return BLOCK_STALLED
    return None


def condition_persists(snap: TaskSnapshot, reason: str) -> bool:
    """Whether an EXISTING block's root cause still holds — used to decide recover vs.
    advance for a task that already carries a block. Deliberately does NOT re-derive a
    stall from ``updated_at``: the recovery ladder's own writes (e.g. clearing the
    assignee on the reassign rung) bump ``updated_at`` via the row trigger, which would
    spuriously reset the stall clock. Instead a stall persists while the task stays
    active — so the ladder escalates a genuinely stuck task to a human — and clears only
    when the task leaves the active set (completed/cancelled) or a definitive condition
    (failed dep / clarification / loop escalation) resolves."""
    if snap.status not in ACTIVE_STATES:
        return False
    if reason == BLOCK_DEP_FAILED:
        return snap.dependency_failed
    if reason == BLOCK_CLARIFICATION:
        return snap.awaiting_clarification
    if reason == BLOCK_LOOP_ESCALATION:
        return snap.loop_escalation
    return reason == BLOCK_STALLED


def next_rung(current: str | None) -> str:
    """The next recovery rung after ``current`` (None ⇒ the first rung). Human
    escalation is terminal — it never advances past itself."""
    if current is None:
        return LADDER[0]
    idx = LADDER.index(current)
    return LADDER[min(idx + 1, len(LADDER) - 1)]


def is_terminal_rung(rung: str) -> bool:
    return rung == RUNG_HUMAN


__all__ = [
    "ACTIVE_STATES",
    "BLOCK_CLARIFICATION",
    "BLOCK_DEP_FAILED",
    "BLOCK_LOOP_ESCALATION",
    "BLOCK_STALLED",
    "LADDER",
    "RUNG_DECOMPOSE",
    "RUNG_DEPENDENCY_RECHECK",
    "RUNG_HUMAN",
    "RUNG_REASSIGN",
    "TaskSnapshot",
    "condition_persists",
    "detect_block",
    "is_terminal_rung",
    "next_rung",
]
