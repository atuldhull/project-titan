"""Pure unit tests for the §7 conflict state machine — no DB, no Redis: the arbiter
appointment rule, the legal transition graph, deadlock escalation, and the rule-5
predicates (Vol 2 §7, TITAN-AGT-017/020)."""

from __future__ import annotations

import pytest

from titan.conflict.state import (
    ALLOWED_TRANSITIONS,
    DEFAULT_MAX_CONFLICT_ROUNDS,
    DeadlockThresholds,
    arbiter_for,
    escalate_one_level,
    is_legal_transition,
    is_terminal,
    reopens_deadlocked,
    round_deadlocked,
)

# --- arbiter appointment (§7.2) --------------------------------------------


def test_arbiter_is_fixed_by_dispute_kind() -> None:
    # The mechanically-checkable core of TITAN-AGT-017: the parties never pick
    # their own judge — the kind determines the arbiter.
    assert arbiter_for("technical") == "architect"
    assert arbiter_for("scope") == "pm_product"
    assert arbiter_for("priority") == "pm_project"
    # A cross-domain dispute has no arbiter — it escalates straight to the CEO.
    assert arbiter_for("cross_domain") is None


def test_arbiter_for_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        arbiter_for("bikeshed")


# --- the legal transition graph (§7 rules 1–3) -----------------------------


def test_legal_transitions_match_the_seven_ladder_moves() -> None:
    assert is_legal_transition("direct", "resolved")  # consensus (rule 1)
    assert is_legal_transition("direct", "arbitration")  # no consensus (rule 2)
    assert is_legal_transition("direct", "executive")  # cross-domain (rule 3)
    assert is_legal_transition("arbitration", "resolved")  # binding verdict
    assert is_legal_transition("arbitration", "executive")  # disputed (rule 3)
    assert is_legal_transition("executive", "resolved")  # CEO final


def test_illegal_transitions_are_rejected() -> None:
    # A resolved conflict is terminal; nothing leaves it.
    assert not is_legal_transition("resolved", "executive")
    assert not is_legal_transition("resolved", "direct")
    # No de-escalation, no self-loop through the normal ladder.
    assert not is_legal_transition("executive", "arbitration")
    assert not is_legal_transition("arbitration", "direct")
    assert not is_legal_transition("direct", "direct")
    # Skipping from executive back is impossible; every allowed pair is enumerated.
    assert len(ALLOWED_TRANSITIONS) == 6


def test_is_terminal() -> None:
    assert is_terminal("resolved")
    assert not is_terminal("direct")
    assert not is_terminal("executive")


# --- deadlock escalation (§7.5) --------------------------------------------


def test_escalate_one_level_walks_up_the_ladder() -> None:
    assert escalate_one_level("direct") == "arbitration"
    assert escalate_one_level("arbitration") == "executive"
    # At the top there is nowhere higher inside the platform — it stays put (and the
    # service flags it for the human dashboard instead).
    assert escalate_one_level("executive") == "executive"


def test_escalate_from_terminal_raises() -> None:
    with pytest.raises(ValueError):
        escalate_one_level("resolved")


# --- rule-5 predicates + thresholds ----------------------------------------


def test_default_thresholds() -> None:
    thresholds = DeadlockThresholds()
    assert thresholds.max_conflict_rounds == DEFAULT_MAX_CONFLICT_ROUNDS == 3
    assert thresholds.max_task_reopens == 3


def test_round_and_reopen_deadlock_predicates() -> None:
    thresholds = DeadlockThresholds(max_conflict_rounds=3, max_task_reopens=3)
    assert not round_deadlocked(2, thresholds)
    assert round_deadlocked(3, thresholds)
    assert round_deadlocked(4, thresholds)  # past the threshold still deadlocked
    assert not reopens_deadlocked(2, thresholds)
    assert reopens_deadlocked(3, thresholds)


def test_thresholds_are_injectable_so_a_change_takes_effect() -> None:
    # A lower threshold trips sooner — the mechanism behind "no redeploy".
    tight = DeadlockThresholds(max_conflict_rounds=2, max_task_reopens=1)
    assert round_deadlocked(2, tight)
    assert reopens_deadlocked(1, tight)
