"""TITAN-SEC-083 loop-detection unit tests.

The pure ``advance`` decision and the ``tool_signature`` fingerprint are the
re-breakable core; the ``LoopGuard`` wrapper is exercised over both its in-process
and Redis backends. The turn-level "runaway halts within N+1 and escalates"
integration lives in ``test_runtime_turn`` (this file proves the mechanism).
"""

import fakeredis.aioredis
import pytest

from titan.runtime.loop_detection import (
    LoopConfig,
    LoopGuard,
    LoopState,
    advance,
    tool_signature,
)

from .conftest import run_async

# --- tool_signature: "near-identical" collapses cosmetic differences --------


def test_signature_is_order_and_whitespace_insensitive() -> None:
    a = tool_signature("fs.read", {"path": "  a/b.py ", "mode": "r"})
    b = tool_signature("fs.read", {"mode": "r", "path": "a/b.py"})  # reordered + trimmed
    assert a == b


def test_signature_distinguishes_real_progress() -> None:
    # An incrementing page is genuine progress, not a loop — it MUST differ.
    assert tool_signature("search", {"page": 1}) != tool_signature("search", {"page": 2})
    # Different tool, same args -> different.
    assert tool_signature("fs.read", {"path": "x"}) != tool_signature("fs.list", {"path": "x"})


# --- advance: the pure decision --------------------------------------------


def _run(sigs: list[str], config: LoopConfig) -> list[str | None]:
    """Reasons emitted feeding ``sigs`` one at a time from a fresh state."""
    state = LoopState()
    reasons: list[str | None] = []
    for sig in sigs:
        state, verdict = advance(state, sig, config)
        reasons.append(verdict.reason)
    return reasons


def test_repeat_trips_on_the_nth_consecutive() -> None:
    cfg = LoopConfig(max_repeats=3, step_budget=999)
    reasons = _run(["a", "a", "a", "a"], cfg)
    assert reasons == [None, None, "repeat", "repeat"]  # trips on the 3rd, not before


def test_a_new_signature_resets_the_consecutive_run() -> None:
    cfg = LoopConfig(max_repeats=3, step_budget=999)
    # a a b a a a -> only the final run of three a's trips.
    reasons = _run(["a", "a", "b", "a", "a", "a"], cfg)
    assert reasons == [None, None, None, None, None, "repeat"]


def test_step_budget_trips_when_exceeded_even_without_repeats() -> None:
    cfg = LoopConfig(max_repeats=99, step_budget=3)
    reasons = _run(["a", "b", "c", "d"], cfg)  # all distinct -> repeat never fires
    assert reasons == [None, None, None, "step_budget"]  # 4th step exceeds budget of 3


def test_repeat_reason_beats_step_budget_when_both_would_trip() -> None:
    cfg = LoopConfig(max_repeats=2, step_budget=1)
    _state, verdict = advance(LoopState(last_signature="a", repeat_count=1, steps=1), "a", cfg)
    assert verdict.tripped and verdict.reason == "repeat"  # specific reason wins


def test_disabled_config_never_trips() -> None:
    cfg = LoopConfig(max_repeats=1, step_budget=0, enabled=False)
    reasons = _run(["a", "a", "a"], cfg)
    assert reasons == [None, None, None]


# --- LoopGuard: over both backends -----------------------------------------


def _guards() -> list[LoopGuard]:
    cfg = LoopConfig(max_repeats=3, step_budget=999)
    return [
        LoopGuard(cfg),  # in-process
        LoopGuard(cfg, redis=fakeredis.aioredis.FakeRedis(decode_responses=True)),  # Redis
    ]


@pytest.mark.parametrize("guard", _guards())
def test_guard_trips_across_turns(guard: LoopGuard) -> None:
    task = "task-1"
    # Each call() is a separate "turn" with one tool call; state must persist.
    assert run_async(lambda: guard.check(task, [("fs.read", {"path": "x"})])) is None
    assert run_async(lambda: guard.check(task, [("fs.read", {"path": "x"})])) is None
    verdict = run_async(lambda: guard.check(task, [("fs.read", {"path": "x"})]))
    assert verdict is not None and verdict.reason == "repeat" and verdict.repeat_count == 3


@pytest.mark.parametrize("guard", _guards())
def test_guard_ignores_taskless_and_empty(guard: LoopGuard) -> None:
    assert run_async(lambda: guard.check(None, [("fs.read", {"path": "x"})])) is None
    assert run_async(lambda: guard.check("t", [])) is None


@pytest.mark.parametrize("guard", _guards())
def test_guard_reset_clears_the_run(guard: LoopGuard) -> None:
    task = "task-2"
    run_async(lambda: guard.check(task, [("t", {"a": 1})]))
    run_async(lambda: guard.check(task, [("t", {"a": 1})]))
    run_async(lambda: guard.reset(task))
    # After reset the run starts over — two more identical calls do NOT trip (N=3).
    assert run_async(lambda: guard.check(task, [("t", {"a": 1})])) is None
    assert run_async(lambda: guard.check(task, [("t", {"a": 1})])) is None


@pytest.mark.parametrize("guard", _guards())
def test_guard_resets_on_trip_so_a_retried_task_is_recoverable(guard: LoopGuard) -> None:
    """Reset-on-trip (review fix): after a trip the per-task run is CLEARED, so a
    legitimately retried/reassigned task starts from a fresh budget instead of
    re-tripping on its very first call forever. If check() persisted the tripped
    state instead, the call right after the trip would immediately re-trip and
    this test would go red — so it guards the fix, not just its shape."""
    task, calls = "retry-me", [("fs.read", {"path": "x"})]
    assert run_async(lambda: guard.check(task, calls)) is None
    assert run_async(lambda: guard.check(task, calls)) is None
    assert run_async(lambda: guard.check(task, calls)) is not None  # trips at N=3
    # Recovered: the next two identical calls do NOT immediately re-trip.
    assert run_async(lambda: guard.check(task, calls)) is None
    assert run_async(lambda: guard.check(task, calls)) is None
    assert run_async(lambda: guard.check(task, calls)) is not None  # a fresh N -> trips again


def test_guard_trips_within_one_turn_of_repeated_calls() -> None:
    # A single response bearing three identical calls trips inside that turn.
    guard = LoopGuard(LoopConfig(max_repeats=3, step_budget=999))
    calls = [("fs.read", {"path": "x"})] * 3
    verdict = run_async(lambda: guard.check("t", calls))
    assert verdict is not None and verdict.reason == "repeat"


def test_disabled_guard_is_transparent() -> None:
    guard = LoopGuard(LoopConfig(enabled=False))
    for _ in range(10):
        assert run_async(lambda: guard.check("t", [("x", {})])) is None
