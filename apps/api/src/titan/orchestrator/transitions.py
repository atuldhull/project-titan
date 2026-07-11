"""The task state machine transition table (TITAN-BCK-012).

The single source of truth for how task state moves: a mapping
``(from_status, action) -> (to_status, required_permission, message_kind)``.
The ``:transition`` endpoint and the scheduler both resolve against this table;
an unresolvable pair is an illegal transition (409). Pure data + lookup — no DB,
no I/O — so the exhaustive (state, action) matrix is trivially unit-testable.
"""

from dataclasses import dataclass

# The task statuses. The Vol 3 §8.1 engineering states (backlog … cancelled) are
# the fine-grained machine; Phase 41 maps the Vol 6 §2.5 delivery states onto the
# SAME single machine, adding the five §2.5-only states (blocked, in_loop, merged,
# failed, escalated) as new values here rather than building a second mechanism.
# `wfl_lane()` projects every physical status onto its §2.5 Kanban lane.
# `cancelled` is fully terminal; `done` is terminal except for `reopen`;
# `escalated` awaits human action; the rest are in-flight.
STATES: tuple[str, ...] = (
    "backlog",
    "ready",
    "assigned",
    "in_progress",
    "in_review",
    "changes_requested",
    "done",
    "reopened",
    "cancelled",
    # Vol 6 §2.5 delivery states (this phase):
    "blocked",
    "in_loop",
    "merged",
    "failed",
    "escalated",
)

# States a task may be cancelled from — every non-terminal state (§8.1
# "any non-terminal state -- cancel --> cancelled"). Not `done`/`cancelled`;
# `failed`/`escalated` route to human handling (§2.5), never a silent cancel.
_CANCELLABLE: tuple[str, ...] = (
    "backlog",
    "ready",
    "assigned",
    "in_progress",
    "in_review",
    "changes_requested",
    "reopened",
    "blocked",
    "in_loop",
    "merged",
)


@dataclass(frozen=True)
class Transition:
    action: str
    from_status: str
    to_status: str
    #: The RBAC permission the caller needs (checked by the router).
    required_permission: str
    #: The messages-row kind written for this transition, or None for a
    #: system transition with no actor (backlog -> ready by the scheduler).
    message_kind: str | None
    #: The BCK-012 actor-role constraint (e.g. a reviewer must be a Code
    #: Reviewer). None = any role. Role-capability enforcement is the scheduler's
    #: job (Phase 14); the review path enforces reviewer != assignee directly, so
    #: this stays advisory here and completes the BCK-012 tuple shape.
    required_actor_role: str | None = None


_TABLE: list[Transition] = [
    # Scheduling: deps met -> ready (system/scheduler-driven; no actor message).
    Transition("ready", "backlog", "ready", "task:transition", None),
    # Assignment (race-safe; sets assignee) and execution.
    Transition("assign", "ready", "assigned", "task:assign", "task_assignment"),
    Transition("start", "assigned", "in_progress", "task:transition", "progress_report"),
    Transition("start", "reopened", "in_progress", "task:transition", "progress_report"),
    Transition("submit_review", "in_progress", "in_review", "task:transition", "review_request"),
    # Review outcomes (reviewer must differ from the assignee — enforced in the
    # service, not the table).
    Transition("approve", "in_review", "done", "task:transition", "review_request"),
    Transition(
        "request_changes", "in_review", "changes_requested", "task:transition", "review_request"
    ),
    Transition("resubmit", "changes_requested", "in_review", "task:transition", "progress_report"),
    # Reopen a completed task (new run attempt; task:reopen).
    Transition("reopen", "done", "reopened", "task:reopen", "bug_report"),
    # -- Vol 6 §2.5 delivery edges (Phase 41), added to the SAME machine -------
    # READY <-> BLOCKED: a ready task whose blocker regressed parks in blocked
    # and returns to ready when unblocked (never skips assignment/single-owner).
    Transition("block", "ready", "blocked", "task:transition", "progress_report"),
    Transition("unblock", "blocked", "ready", "task:transition", "progress_report"),
    # IN_PROGRESS -> IN_LOOP -> MERGED -> DONE: the self-improvement loop path.
    Transition("enter_loop", "in_progress", "in_loop", "task:transition", "progress_report"),
    Transition("merge", "in_loop", "merged", "task:transition", "progress_report"),
    # MERGED -> DONE via `complete`: the §2.5 done-entry that requires the parent
    # story's acceptance criteria to pass (enforced in the service, TITAN-WFL-009).
    Transition("complete", "merged", "done", "task:transition", "review_request"),
    # IN_PROGRESS -> FAILED -> ESCALATED and IN_LOOP -> ESCALATED (§2.5).
    Transition("fail", "in_progress", "failed", "task:transition", "progress_report"),
    Transition("escalate", "failed", "escalated", "task:transition", "progress_report"),
    Transition("escalate", "in_loop", "escalated", "task:transition", "progress_report"),
    # REOPENED -> READY (§2.5): a reopened task re-enters the ready pool. (The
    # engineering machine also allows reopened -> in_progress via `start`.)
    Transition("requeue", "reopened", "ready", "task:transition", "progress_report"),
]
# Cancel from every non-terminal state.
_TABLE += [
    Transition("cancel", state, "cancelled", "task:cancel", "progress_report")
    for state in _CANCELLABLE
]

TRANSITIONS: dict[tuple[str, str], Transition] = {(t.from_status, t.action): t for t in _TABLE}

#: Every distinct action name (for exhaustive-matrix tests and validation).
ACTIONS: tuple[str, ...] = tuple(sorted({t.action for t in _TABLE}))


def resolve(from_status: str, action: str) -> Transition | None:
    """The legal transition for ``(from_status, action)``, or None if illegal."""
    return TRANSITIONS.get((from_status, action))


# --- Vol 6 §2.5 Kanban lane projection -------------------------------------

# The pseudo-state a task on an active budget hold reports (TITAN-INT-064). It is
# an ORTHOGONAL overlay, NOT a physical status: the task keeps its real status so
# raising the budget resumes it exactly where it paused (§ point 4).
BUDGET_HOLD_LANE = "BUDGET_HOLD"

# Every physical status → its Vol 6 §2.5 lane. The fine-grained engineering states
# fold onto the coarser delivery lanes (assigned/in_progress → IN_PROGRESS;
# in_review/changes_requested/in_loop → IN_LOOP). `cancelled` has no §2.5 lane —
# it is a deliberate terminal reported as CANCELLED.
_WFL_LANE: dict[str, str] = {
    "backlog": "BACKLOG",
    "ready": "READY",
    "blocked": "BLOCKED",
    "assigned": "IN_PROGRESS",
    "in_progress": "IN_PROGRESS",
    "in_review": "IN_LOOP",
    "changes_requested": "IN_LOOP",
    "in_loop": "IN_LOOP",
    "merged": "MERGED",
    "done": "DONE",
    "reopened": "REOPENED",
    "failed": "FAILED",
    "escalated": "ESCALATED",
    "cancelled": "CANCELLED",
}


def wfl_lane(status: str, *, budget_hold: bool = False) -> str:
    """Project a physical task ``status`` onto its Vol 6 §2.5 Kanban lane. When
    ``budget_hold`` is set the task reports ``BUDGET_HOLD`` regardless of its
    (preserved, resumable) underlying status."""
    if budget_hold:
        return BUDGET_HOLD_LANE
    return _WFL_LANE.get(status, status.upper())
