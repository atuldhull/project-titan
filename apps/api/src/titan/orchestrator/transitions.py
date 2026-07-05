"""The task state machine transition table (TITAN-BCK-012).

The single source of truth for how task state moves: a mapping
``(from_status, action) -> (to_status, required_permission, message_kind)``.
The ``:transition`` endpoint and the scheduler both resolve against this table;
an unresolvable pair is an illegal transition (409). Pure data + lookup — no DB,
no I/O — so the exhaustive (state, action) matrix is trivially unit-testable.
"""

from dataclasses import dataclass

# The 9 task statuses (§8.1). `cancelled` is fully terminal; `done` is terminal
# except for `reopen`; the rest are in-flight.
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
)

# States a task may be cancelled from — every non-terminal state (§8.1
# "any non-terminal state -- cancel --> cancelled"). Not `done`/`cancelled`.
_CANCELLABLE: tuple[str, ...] = (
    "backlog",
    "ready",
    "assigned",
    "in_progress",
    "in_review",
    "changes_requested",
    "reopened",
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
