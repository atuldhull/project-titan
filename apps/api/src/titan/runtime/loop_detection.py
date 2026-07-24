"""Agent loop detection (TITAN-SEC-083).

A runaway agent repeats the same tool call over and over, or grinds through an
unbounded number of steps without finishing. Two triggers, checked at the tool
chokepoint of every turn BEFORE the offending call runs, so the task halts within
N+1 iterations rather than after the damage:

* **repeat** — ≥ ``max_repeats`` (default 3) near-identical CONSECUTIVE tool
  calls for a task;
* **step budget** — more than ``step_budget`` total tool calls for a task.

"Near-identical" is realised as canonical equality: arguments are compared with
their keys sorted and string whitespace collapsed, so a model re-emitting the
same call with cosmetic differences still counts, while genuine progress (an
incrementing page, a new path) does NOT — a false pause is disruptive and a true
runaway repeats verbatim, so the comparison is deliberately conservative.

The counters live per task (Redis when available, else in-process) and carry a
TTL, so a paused/abandoned task's state self-cleans and a fresh signature resets
the consecutive run on its own. The pure :func:`advance` core is the unit-tested
decision; the guard is a thin, boundary-free wrapper around it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from redis.asyncio import Redis

logger = structlog.get_logger("titan.runtime.loop")

_WS = re.compile(r"\s+")
# Per-task loop state lives at most this long; each observation refreshes it, so
# an active task keeps its run while a stalled/paused one expires on its own.
_STATE_TTL_S = 30 * 60
# Bound on the in-process fallback map (no Redis): evict the oldest task states.
_MEM_CAP = 4096

LoopReason = Literal["repeat", "step_budget"]


@dataclass(frozen=True)
class LoopConfig:
    """Loop-detection thresholds (TITAN-SEC-083). ``enabled=False`` makes every
    check a no-op (the guard is transparent)."""

    max_repeats: int = 3
    step_budget: int = 120
    enabled: bool = True


@dataclass(frozen=True)
class LoopState:
    """Per-task detector state: the last tool signature seen, how many times it
    has appeared consecutively (incl. the latest), and the total steps so far."""

    last_signature: str | None = None
    repeat_count: int = 0
    steps: int = 0


@dataclass(frozen=True)
class LoopVerdict:
    tripped: bool
    reason: LoopReason | None
    signature: str
    repeat_count: int
    steps: int


def _normalize(value: Any) -> Any:
    """Collapse cosmetic differences so "near-identical" calls hash the same:
    strip + single-space string runs; recurse into dicts/lists; leave
    numbers/bools/None (semantic content) untouched."""
    if isinstance(value, str):
        return _WS.sub(" ", value.strip())
    if isinstance(value, Mapping):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def tool_signature(tool: str, args: Mapping[str, Any]) -> str:
    """A stable, order/whitespace-insensitive fingerprint of a tool call."""
    canon = json.dumps(_normalize(dict(args)), sort_keys=True, separators=(",", ":"), default=str)
    return f"{tool}:{hashlib.sha256(canon.encode('utf-8')).hexdigest()[:16]}"


def advance(state: LoopState, signature: str, config: LoopConfig) -> tuple[LoopState, LoopVerdict]:
    """The pure decision: fold one tool-call signature into the state and report
    whether it trips a trigger. A repeat extends the consecutive run; a new
    signature restarts it at 1. ``steps`` always advances. Repeat is checked
    before step-budget so the more specific reason wins."""
    steps = state.steps + 1
    repeat = state.repeat_count + 1 if signature == state.last_signature else 1
    reason: LoopReason | None = None
    if config.enabled:
        if repeat >= config.max_repeats:
            reason = "repeat"
        elif steps > config.step_budget:
            reason = "step_budget"
    new = LoopState(last_signature=signature, repeat_count=repeat, steps=steps)
    return new, LoopVerdict(reason is not None, reason, signature, repeat, steps)


class LoopGuard:
    """Per-task loop tracker over :func:`advance`. Redis-backed when a client is
    given (shared across workers), else an in-process bounded map (still spans a
    long-lived worker's turns). Task-less triggers are not tracked."""

    def __init__(self, config: LoopConfig, redis: Redis | None = None) -> None:
        self._config = config
        self._redis = redis
        self._mem: OrderedDict[str, LoopState] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def _key(self, task_id: str) -> str:
        return f"loop:state:{task_id}"

    async def _get(self, task_id: str) -> LoopState:
        if self._redis is not None:
            raw = await self._redis.hgetall(self._key(task_id))
            if raw:
                sig = raw.get("sig")
                return LoopState(
                    last_signature=sig if isinstance(sig, str) and sig else None,
                    repeat_count=int(raw.get("repeat", 0)),
                    steps=int(raw.get("steps", 0)),
                )
            return LoopState()
        return self._mem.get(task_id, LoopState())

    async def _put(self, task_id: str, state: LoopState) -> None:
        if self._redis is not None:
            key = self._key(task_id)
            await self._redis.hset(
                key,
                mapping={
                    "sig": state.last_signature or "",
                    "repeat": state.repeat_count,
                    "steps": state.steps,
                },
            )
            await self._redis.expire(key, _STATE_TTL_S)
            return
        self._mem[task_id] = state
        self._mem.move_to_end(task_id)
        while len(self._mem) > _MEM_CAP:
            self._mem.popitem(last=False)

    async def check(
        self, task_id: object, calls: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> LoopVerdict | None:
        """Fold this turn's requested tool calls (in order) into the task's state,
        returning the FIRST verdict that trips (and stopping there) or None. The
        state is persisted so the next turn continues the same consecutive run —
        cross-turn loops are caught, not just within-turn ones."""
        if not self._config.enabled or task_id is None or not calls:
            return None
        key = str(task_id)
        # Best-effort like the budget meter: a Redis hiccup DEGRADES loop detection
        # for this round (the turn proceeds, still bounded by max_tool_iterations +
        # the fail-closed budget caps) rather than FAILING the turn (review).
        try:
            state = await self._get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("loop.check_read_failed", error=str(exc))
            return None
        tripped: LoopVerdict | None = None
        for tool, args in calls:
            state, verdict = advance(state, tool_signature(tool, args), self._config)
            if verdict.tripped:
                tripped = verdict
                break
        try:
            if tripped is not None:
                # The guard has done its job (the turn escalates for review). CLEAR
                # the per-task state so a legitimate retry/reassign of THIS task
                # starts from a fresh budget instead of re-tripping on its very
                # first call forever — the Redis TTL is refreshed on every check, so
                # a halted task would otherwise be permanently stuck (review: dead
                # reset()/unrecoverable trap). A task that truly keeps looping is
                # re-caught within N calls each drive, bounded by the recovery
                # ladder's terminal escalation.
                await self.reset(task_id)
            else:
                await self._put(key, state)
        except Exception as exc:  # noqa: BLE001 — a persistence blip never fails the turn
            logger.warning("loop.check_write_failed", error=str(exc))
        return tripped

    async def reset(self, task_id: object) -> None:
        """Forget a task's loop state. Called automatically after a trip (so a
        re-driven task is recoverable) and available for an explicit resume."""
        if task_id is None:
            return
        key = str(task_id)
        if self._redis is not None:
            await self._redis.delete(self._key(key))
        else:
            self._mem.pop(key, None)


__all__ = [
    "LoopConfig",
    "LoopGuard",
    "LoopReason",
    "LoopState",
    "LoopVerdict",
    "advance",
    "tool_signature",
]
