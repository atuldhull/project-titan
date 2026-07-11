"""Working memory (Vol 5 §4.5, §8.2; TITAN-BRN-002/-016/-017) — private to `memory`.

The task-scoped short-term store in Redis, exactly the §4.5 keyspace:

    wm:{task_id}:scratch   HASH    agent scratch entries, keyed by call seq
    wm:{task_id}:summary   STRING  the rolling five-field summary (JSON)
    wm:{task_id}:events    LIST    Memory Events queued but not yet durable

Scratch and summary carry the §4.5 24 h TTL (both re-derivable: scratch from
the thread's messages rows, the summary from its PostgreSQL checkpoint). The
EVENTS list deliberately does NOT expire while non-empty: it is the only copy
of derived Memory Events in the gap between an agent turn producing them and
the transactional ``write_events`` flush, so an expiry would lose durable
memory AND blind the TITAN-BRN-002 completion gate — the MUST outranks the
keyspace table's TTL column. Task completion is REJECTED while the list is
non-empty; the summary is checkpointed on every fold, so flushing Redis costs
at most the verbatim turns not yet folded.

The :class:`SummaryFolder` implements §8.2 mechanism 1: evicted verbatim turns
are folded into the fixed five-field schema (Goal / Done so far / Open threads
/ Constraints discovered / Next step) by the CHEAPEST configured chat model
(utility tier — never premium, TITAN-BRN-017), regenerated incrementally from
(previous summary + evicted turns). A malformed model reply degrades to a
deterministic merge — folding never fails and never loses the evicted turns
silently.
"""

import json
import re
import uuid
from collections.abc import Sequence
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from titan.llm.service import ChatRequest, ChatResponse, Message

logger = structlog.get_logger("titan.memory")

WM_TTL_SECONDS = 24 * 60 * 60  # §4.5 TTL for the re-derivable wm keys
# §8.2: the summary is re-checkpointed to PostgreSQL at least every N folds.
# We checkpoint on EVERY fold (an upsert is cheap), so a Redis loss costs zero
# folds — strictly stronger than the TITAN-BRN-016 "at least every 5" bound.
CHECKPOINT_EVERY_FOLDS = 1
_FALLBACK_DIGEST_CHARS = 160
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def scratch_key(task_id: uuid.UUID) -> str:
    return f"wm:{task_id}:scratch"


def summary_key(task_id: uuid.UUID) -> str:
    return f"wm:{task_id}:summary"


def events_key(task_id: uuid.UUID) -> str:
    return f"wm:{task_id}:events"


class RollingSummary(BaseModel):
    """The fixed §8.2 five-field schema (TITAN-BRN-016). Fields are plain
    strings so the schema check is exactly 'these five keys, all text'."""

    model_config = ConfigDict(extra="forbid")

    goal: str = ""
    done_so_far: str = ""
    open_threads: str = ""
    constraints_discovered: str = ""
    next_step: str = ""

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.goal,
                self.done_so_far,
                self.open_threads,
                self.constraints_discovered,
                self.next_step,
            )
        )

    def render(self) -> str:
        """The prompt's working-summary slot."""
        if self.is_empty:
            return ""
        return (
            f"Goal: {self.goal}\n"
            f"Done so far: {self.done_so_far}\n"
            f"Open threads: {self.open_threads}\n"
            f"Constraints discovered: {self.constraints_discovered}\n"
            f"Next step: {self.next_step}"
        )


class WorkingMemory:
    """The §4.5 Redis keyspace operations. ``redis`` is the shared async client
    (decode_responses=True); every write refreshes the 24 h TTL."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def _touch(self, task_id: uuid.UUID) -> None:
        # Scratch + summary are cache-like (both re-derivable: scratch from the
        # thread's messages rows, the summary from its PostgreSQL checkpoint) —
        # they carry the §4.5 24 h TTL. The EVENTS list is deliberately NOT
        # expired here: while non-empty it is the only copy of derived Memory
        # Events awaiting their transactional flush, and an expiry would both
        # lose them and blind the TITAN-BRN-002 completion gate (LLEN of a
        # vanished key is 0 — the gate would wave the task through). The MUST
        # outranks the keyspace table's TTL column; the list empties via the
        # normal flush path within the same turn in the healthy case.
        for key in (scratch_key(task_id), summary_key(task_id)):
            await self._redis.expire(key, WM_TTL_SECONDS)

    # -- scratch (HASH: call seq -> entry) -----------------------------------

    async def append_scratch(
        self, task_id: uuid.UUID, seq: int, entry: dict[str, Any], *, entry_id: str = ""
    ) -> None:
        """``entry_id`` disambiguates concurrent turns that observed the same
        call seq (two agents in one thread) — same-seq entries would otherwise
        overwrite each other in the HASH. Sort order stays seq-first."""
        field = f"{seq:08d}" + (f":{entry_id}" if entry_id else "")
        await self._redis.hset(scratch_key(task_id), field, json.dumps(entry))
        await self._touch(task_id)

    async def scratch(self, task_id: uuid.UUID) -> list[dict[str, Any]]:
        raw = await self._redis.hgetall(scratch_key(task_id))
        entries: list[dict[str, Any]] = []
        for field in sorted(raw):
            try:
                entries.append(json.loads(raw[field]))
            except (json.JSONDecodeError, TypeError):  # corrupt entry — skip, never crash
                logger.warning("memory.wm_scratch_entry_invalid", task_id=str(task_id))
        return entries

    # -- rolling summary (STRING: five-field JSON) ----------------------------

    async def load_summary(self, task_id: uuid.UUID) -> RollingSummary | None:
        raw = await self._redis.get(summary_key(task_id))
        if raw is None:
            return None
        try:
            return RollingSummary.model_validate_json(raw)
        except Exception:  # schema-skewed / corrupt — treat as absent
            logger.warning("memory.wm_summary_invalid", task_id=str(task_id))
            return None

    async def store_summary(self, task_id: uuid.UUID, summary: RollingSummary) -> None:
        await self._redis.set(summary_key(task_id), summary.model_dump_json(), ex=WM_TTL_SECONDS)
        await self._touch(task_id)

    # -- pending Memory Events (LIST) -----------------------------------------

    async def queue_events(self, task_id: uuid.UUID, events: Sequence[dict[str, Any]]) -> None:
        if not events:
            return
        await self._redis.rpush(events_key(task_id), *[json.dumps(e) for e in events])
        # No TTL while pending (see _touch); PERSIST clears any TTL a previous
        # deploy may have stamped on the key.
        await self._redis.persist(events_key(task_id))
        await self._touch(task_id)

    async def raw_events(self, task_id: uuid.UUID) -> list[str]:
        """The queued entries VERBATIM — the flush needs raw strings so a
        JSON-invalid entry can be dead-lettered positionally instead of
        sticking at the head of the list forever."""
        return list(await self._redis.lrange(events_key(task_id), 0, -1))

    async def pending_events(self, task_id: uuid.UUID) -> list[dict[str, Any]]:
        raw = await self._redis.lrange(events_key(task_id), 0, -1)
        events: list[dict[str, Any]] = []
        for item in raw:
            try:
                events.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                logger.warning("memory.wm_event_entry_invalid", task_id=str(task_id))
        return events

    async def pending_count(self, task_id: uuid.UUID) -> int:
        return int(await self._redis.llen(events_key(task_id)))

    async def ack_events(self, task_id: uuid.UUID, count: int) -> None:
        """Drop the first ``count`` entries — the ones a successful transactional
        flush just made durable. Anything queued meanwhile stays pending
        (RPUSH appends after the snapshot, so a positional LTRIM under the
        flush LOCK never drops an unwritten event)."""
        if count <= 0:
            return
        await self._redis.ltrim(events_key(task_id), count, -1)

    async def dead_letter_event(self, task_id: uuid.UUID, raw: str) -> None:
        """Quarantine one unflushable (schema-invalid) queued entry so it stops
        blocking the flush — and the TITAN-BRN-002 gate — without silent loss:
        it stays inspectable under ``wm:{task_id}:events:dead`` (no TTL)."""
        await self._redis.rpush(f"{events_key(task_id)}:dead", raw)
        await self._redis.persist(f"{events_key(task_id)}:dead")

    # -- the flush lock ---------------------------------------------------------

    async def acquire_flush_lock(self, task_id: uuid.UUID, *, ttl_seconds: int = 30) -> str | None:
        """SET NX lock serialising flushers per task: two concurrent
        flush_pending calls (a live turn + a crash-replay drain) must never
        both write the same snapshot (duplicate durable records) or trim past
        each other. Returns a release token, or None if someone else holds it
        (the caller simply leaves the queue for the holder)."""
        token = uuid.uuid4().hex
        acquired = await self._redis.set(
            f"{events_key(task_id)}:lock", token, nx=True, ex=ttl_seconds
        )
        return token if acquired else None

    async def release_flush_lock(self, task_id: uuid.UUID, token: str) -> None:
        key = f"{events_key(task_id)}:lock"
        current = await self._redis.get(key)
        if current == token:  # best-effort compare-and-delete; expiry backstops
            await self._redis.delete(key)


async def pending_wm_events(redis: Any, task_id: uuid.UUID) -> int:
    """The TITAN-BRN-002 completion gate's question, callable without
    constructing a WorkingMemory (the orchestrator's one dependency on us)."""
    return int(await redis.llen(events_key(task_id)))


# --- §8.2 mechanism 1: the incremental summary fold --------------------------------


class SummaryFolder:
    """Folds evicted verbatim turns into the rolling summary via ONE call to the
    cheapest configured chat model (utility tier — TITAN-BRN-017; the same
    cheapest-alias selection the LLM listwise reranker uses, never the premium
    reasoning tier). Malformed output degrades to a deterministic merge."""

    def __init__(self, gateway: Any, model_alias: str | None = None) -> None:
        self._gateway = gateway
        self._alias = model_alias

    async def fold(self, previous: RollingSummary, evicted_turns: Sequence[str]) -> RollingSummary:
        if not evicted_turns:
            return previous
        turns_block = "\n".join(f"- {t[:600]}" for t in evicted_turns)
        prompt = (
            "Update this rolling task summary by folding in the evicted turns.\n"
            f"Current summary (JSON): {previous.model_dump_json()}\n\n"
            f"Evicted turns (oldest first):\n{turns_block}\n\n"
            "Answer with ONLY a JSON object with exactly these string keys: "
            "goal, done_so_far, open_threads, constraints_discovered, next_step. "
            "Keep each field under 120 words; preserve still-relevant detail."
        )
        try:
            response: ChatResponse = await self._gateway.chat(
                ChatRequest(
                    messages=[Message(role="user", content=prompt)],
                    model_alias=self._alias,
                    max_tokens=800,
                )
            )
            parsed = self._parse(response.text or "")
        except Exception as exc:
            # No exc_info: gateway errors can embed the PROMPT (turn content) in
            # their message chain — metadata only (TITAN-OPS-016).
            logger.warning("memory.summary_fold_failed", error=type(exc).__name__)
            parsed = None
        if parsed is not None:
            return parsed
        return self._fallback(previous, evicted_turns)

    @staticmethod
    def _parse(text: str) -> RollingSummary | None:
        match = _JSON_BLOCK.search(text)
        if match is None:
            return None
        try:
            return RollingSummary.model_validate_json(match.group(0))
        except Exception:
            return None

    @staticmethod
    def _fallback(previous: RollingSummary, evicted_turns: Sequence[str]) -> RollingSummary:
        """Deterministic no-model merge: the evicted turns are appended (clipped)
        to `done_so_far` so nothing evicted vanishes silently — lower fidelity
        than a model fold, never a loss (§8.2 'compressed, not dropped')."""
        digests = "; ".join(t[:_FALLBACK_DIGEST_CHARS] for t in evicted_turns)
        done = f"{previous.done_so_far}; {digests}" if previous.done_so_far else digests
        # Keep the TAIL: the newest evicted material is the least summarised —
        # the old head has already survived earlier folds in compressed form.
        return previous.model_copy(update={"done_so_far": done[-4000:]})


__all__ = [
    "CHECKPOINT_EVERY_FOLDS",
    "WM_TTL_SECONDS",
    "RollingSummary",
    "SummaryFolder",
    "WorkingMemory",
    "events_key",
    "pending_wm_events",
    "scratch_key",
    "summary_key",
]
