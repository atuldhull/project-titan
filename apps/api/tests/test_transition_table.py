"""Exhaustive task state-machine matrix (TITAN-BCK-012). Pure unit test over the
transition table — no DB — so every (state, action) pair is covered: each legal
move resolves to the documented target, and every other pair is illegal (resolve
-> None, which the :transition endpoint renders as 409). Covers both the §8.1
engineering edges and the Vol 6 §2.5 delivery edges (block/loop/merge/escalate,
Phase 41)."""

from titan.orchestrator.transitions import ACTIONS, STATES, resolve

# The complete set of legal transitions: (from_status, action) -> to_status.
LEGAL: dict[tuple[str, str], str] = {
    # --- §8.1 engineering edges ---
    ("backlog", "ready"): "ready",
    ("ready", "assign"): "assigned",
    ("assigned", "start"): "in_progress",
    ("reopened", "start"): "in_progress",
    ("in_progress", "submit_review"): "in_review",
    ("in_review", "approve"): "done",
    ("in_review", "request_changes"): "changes_requested",
    ("changes_requested", "resubmit"): "in_review",
    ("done", "reopen"): "reopened",
    # --- Vol 6 §2.5 delivery edges (Phase 41) ---
    ("ready", "block"): "blocked",
    ("blocked", "unblock"): "ready",
    ("in_progress", "enter_loop"): "in_loop",
    ("in_loop", "merge"): "merged",
    ("merged", "complete"): "done",
    ("in_progress", "fail"): "failed",
    ("failed", "escalate"): "escalated",
    ("in_loop", "escalate"): "escalated",
    ("reopened", "requeue"): "ready",
    # --- cancel from every non-terminal state (§8.1 + §2.5) ---
    ("backlog", "cancel"): "cancelled",
    ("ready", "cancel"): "cancelled",
    ("assigned", "cancel"): "cancelled",
    ("in_progress", "cancel"): "cancelled",
    ("in_review", "cancel"): "cancelled",
    ("changes_requested", "cancel"): "cancelled",
    ("reopened", "cancel"): "cancelled",
    ("blocked", "cancel"): "cancelled",
    ("in_loop", "cancel"): "cancelled",
    ("merged", "cancel"): "cancelled",
}


def test_every_state_action_pair_is_exhaustively_classified() -> None:
    for state in STATES:
        for action in ACTIONS:
            transition = resolve(state, action)
            if (state, action) in LEGAL:
                assert transition is not None, f"{state} + {action} should be legal"
                assert transition.to_status == LEGAL[(state, action)]
                assert transition.from_status == state and transition.action == action
            else:
                assert transition is None, f"{state} + {action} must be illegal (got {transition})"


def test_terminal_states_have_no_exit_except_reopen() -> None:
    # cancelled is fully terminal; done exits only via reopen.
    assert all(resolve("cancelled", action) is None for action in ACTIONS)
    assert [a for a in ACTIONS if resolve("done", a) is not None] == ["reopen"]


def test_required_permissions_match_the_action() -> None:
    assert resolve("ready", "assign").required_permission == "task:assign"  # type: ignore[union-attr]
    assert resolve("done", "reopen").required_permission == "task:reopen"  # type: ignore[union-attr]
    assert resolve("backlog", "cancel").required_permission == "task:cancel"  # type: ignore[union-attr]
    assert resolve("backlog", "ready").required_permission == "task:transition"  # type: ignore[union-attr]


def test_system_ready_transition_writes_no_message() -> None:
    # backlog -> ready is scheduler-driven (no actor), so it records no message.
    assert resolve("backlog", "ready").message_kind is None  # type: ignore[union-attr]
    assert resolve("ready", "assign").message_kind == "task_assignment"  # type: ignore[union-attr]
