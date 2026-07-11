"""The Memory Packet hook around one agent turn (Vol 5 §13, TITAN-BRN-009).

Volume 5 mandates that every agent LLM call receive its context through a typed,
cited **Memory Packet** assembled by the retrieval pipeline — never an ad-hoc raw
dump of history (TITAN-BRN-009). The real retrieval (hybrid dense + lexical → RRF
→ rerank → assemble) lands in the memory phases; Phase 29 wires only the *seam*:

- :class:`MemoryPacket` — the §6.2 typed structure the runtime splices into the
  prompt's memory slot (sections ordered procedural → semantic → episodic so
  standards dominate anecdotes, with a non-evictable ``identity_context``).
- :class:`MemoryProvider` — the async interface the turn loop calls once per turn.
- :class:`NullMemoryProvider` — the stub that returns a valid *empty* packet with
  a fresh ``packet_id`` so each turn already links to exactly one packet (the
  TITAN-BRN-009 acceptance), long before real retrieval exists.

The packet's ``render`` produces ONLY the text that goes into the prompt; nothing
here is ever written to a log or a span (TITAN-OPS-016).
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.db import uuid7
from titan.memory import service as memory_service

logger = structlog.get_logger("titan.runtime.memory")


class Citation(BaseModel):
    """Provenance for one included chunk so agents can cite memory in decisions
    and reviewers can audit it (§6.2 rule a)."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    chunk_id: str | None = None
    score: float | None = None


class MemoryPacketSections(BaseModel):
    """The §6.2 sections. ``identity_context`` is non-evictable; the three list
    sections are rendered procedural → semantic → episodic on purpose."""

    model_config = ConfigDict(extra="forbid")

    identity_context: str = ""
    procedural: tuple[str, ...] = ()
    semantic: tuple[str, ...] = ()
    episodic: tuple[str, ...] = ()
    working_summary: str = ""


class MemoryPacket(BaseModel):
    """A typed, cited, budget-bounded context bundle for one agent call (§6.2)."""

    model_config = ConfigDict(extra="forbid")

    packet_id: uuid.UUID = Field(default_factory=uuid7)
    agent_id: str
    task_id: uuid.UUID | None = None
    call_seq: int = 0
    sections: MemoryPacketSections = Field(default_factory=MemoryPacketSections)
    citations: tuple[Citation, ...] = ()
    token_cost: int = 0
    budget_tokens: int = 4000

    def render(self) -> str:
        """The prompt's memory slot: identity first (non-evictable), then
        procedural → semantic → episodic → working summary. Empty when the packet
        carries nothing (the null-provider case), so the slot stays blank rather
        than emitting a misleading heading."""
        blocks: list[str] = []
        s = self.sections
        if s.identity_context:
            blocks.append(s.identity_context)
        for label, items in (
            ("Procedural (standards & checklists)", s.procedural),
            ("Semantic (facts & component map)", s.semantic),
            ("Episodic (recent relevant events)", s.episodic),
        ):
            if items:
                lines = "\n".join(f"- {item}" for item in items)
                blocks.append(f"{label}:\n{lines}")
        if s.working_summary:
            blocks.append(f"Working summary:\n{s.working_summary}")
        return "\n\n".join(blocks)


class MemoryRequest(BaseModel):
    """What the runtime asks the memory service to assemble a packet for (§13
    step 1: ``Memory Packet(task, agent, budget)``)."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    call_seq: int = 0
    query: str = ""
    budget_tokens: int = 4000


class MemoryProvider(Protocol):
    """The one method the turn loop needs. The memory phases supply a real
    implementation (Qdrant + Postgres FTS + rerank) conforming to this Protocol
    and inject it into the runtime — no turn-loop change."""

    async def assemble(self, request: MemoryRequest) -> MemoryPacket: ...


class NullMemoryProvider:
    """The Phase-29 stub: a valid, empty, freshly-identified packet every turn.

    It satisfies TITAN-BRN-009's "every agent call links to exactly one
    ``packet_id``" without pretending to retrieve anything — the memory slot
    renders blank until real retrieval lands.
    """

    async def assemble(self, request: MemoryRequest) -> MemoryPacket:
        return MemoryPacket(
            agent_id=request.agent_id,
            task_id=request.task_id,
            call_seq=request.call_seq,
            budget_tokens=request.budget_tokens,
        )


# --- Phase 38: the real provider + write-back coordination (§13) --------------


class PacketMemoryProvider:
    """§13 steps 1–5: assemble the turn's ONE Memory Packet through the
    Phase-36 packet pipeline (the same code POST /memory/packets serves — the
    monolith's runtime embeds the Memory Service, the HTTP surface exists for
    external callers). Never raises into the turn: retrieval trouble degrades
    to an EMPTY packet with a fresh packet_id, so the turn still links to
    exactly one packet (TITAN-BRN-009) and the agent still acts."""

    def __init__(
        self, engine: AsyncEngine, redis: Any, qdrant: Any, embeddings: Any, reranker: Any
    ) -> None:
        self._engine = engine
        self._redis = redis
        self._qdrant = qdrant
        self._embeddings = embeddings
        self._reranker = reranker

    async def assemble(self, request: MemoryRequest) -> MemoryPacket:
        query = request.query.strip() or f"context for agent {request.agent_id}"
        try:
            out = await memory_service.assemble_packet(
                self._engine,
                self._redis,
                self._qdrant,
                self._embeddings,
                self._reranker,
                memory_service.PacketRequest(
                    query=query[:4000],
                    scope=f"project:{request.project_id}",
                    kinds=[],
                    budget_tokens=max(256, min(request.budget_tokens, 32_000)),
                    assembled_for=memory_service.AssembledFor(
                        agent=f"agent.{request.agent_id}",
                        task_id=request.task_id,
                        call_seq=request.call_seq,
                    ),
                ),
            )
        except Exception:
            logger.exception("runtime.memory_packet_failed_degrading_to_empty")
            return MemoryPacket(
                agent_id=request.agent_id,
                task_id=request.task_id,
                call_seq=request.call_seq,
                budget_tokens=request.budget_tokens,
            )
        return MemoryPacket(
            packet_id=out.packet_id,
            agent_id=request.agent_id,
            task_id=request.task_id,
            call_seq=request.call_seq,
            sections=MemoryPacketSections(
                identity_context=out.sections.identity_context,
                procedural=tuple(out.sections.procedural),
                semantic=tuple(out.sections.semantic),
                episodic=tuple(out.sections.episodic),
                working_summary=out.sections.working_summary,
            ),
            citations=tuple(
                Citation(
                    record_id=str(c.record_id),
                    chunk_id=str(c.chunk_id),
                    score=c.score,
                )
                for c in out.citations
            ),
            token_cost=out.token_cost,
            budget_tokens=out.budget_tokens,
        )


class KgEdgeEvent(BaseModel):
    """One §13 step-11 knowledge-graph edge event (typed; the KG phase gives it
    a real sink). ``kind`` names the §7 edge family the message implies."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["decision", "component", "lesson"]
    label: str
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    source_agent: str = ""


class KgEventSink(Protocol):
    """Where §13 step-11 KG edge events go. The knowledge-graph phase supplies
    the real implementation; Phase 38 ships the typed no-op."""

    async def emit(self, events: Sequence[KgEdgeEvent]) -> None: ...


class NoopKgSink:
    """The Phase-38 stand-in: accepts (and counts) the typed events so the §13
    wiring exists end-to-end; the KG phase swaps in graph writes."""

    def __init__(self) -> None:
        self.accepted = 0

    async def emit(self, events: Sequence[KgEdgeEvent]) -> None:
        self.accepted += len(events)


# Message kinds that §13 step 11 turns into durable Memory Events: decisions
# and conflict resolutions are episodic ("decision made / review verdict");
# meeting summaries carry distilled facts (semantic).
_EPISODIC_TYPES = frozenset({"DecisionLog", "ConflictResolution"})
_SEMANTIC_TYPES = frozenset({"MeetingSummary"})
_EVENT_BODY_CHARS = 4_000
# Deterministic write-back client keys: uuid5(ns, emitted message id). The
# same message always derives the same key, so replays/retries dedup.
WM_EVENT_NAMESPACE = uuid.UUID("2b1f7c44-9a1d-5e3b-8c6f-4d2a90e17b38")


@dataclass(frozen=True)
class WorkingState:
    """One task's hydrated working-memory bookkeeping (see ``hydrate``)."""

    summary: memory_service.RollingSummary
    fold_count: int
    folded_through: int  # thread lines already folded (the §8.2 watermark)
    healthy: bool  # the PG checkpoint was readable — folding may persist


@dataclass(frozen=True)
class _ReplayMessage:
    """The minimal (type, payload) shape derive_events needs, rebuilt from a
    committed messages row on the crash-replay path."""

    type: str
    payload: dict[str, Any]


class WorkingMemoryCoordinator:
    """§13 steps 9–13 around one turn: scratch append, rolling-summary folds
    with PostgreSQL checkpoints (TITAN-BRN-016), Memory-Event derivation +
    transactional flush (TITAN-BRN-002/-024), and the KG hook. Every method is
    exception-guarded: write-back trouble is logged and leaves events QUEUED in
    Redis — the completion gate then blocks `done` until a later flush drains
    them — but never fails the turn whose messages already committed."""

    def __init__(
        self,
        engine: AsyncEngine,
        wm: memory_service.WorkingMemory,
        folder: memory_service.SummaryFolder,
        kg: KgEventSink | None = None,
    ) -> None:
        self.engine = engine
        self.wm = wm
        self.folder = folder
        self.kg: KgEventSink = kg if kg is not None else NoopKgSink()

    # -- hydration (§13 step 4) ------------------------------------------------

    async def hydrate(self, task_id: uuid.UUID) -> WorkingState:
        """The rolling summary + fold bookkeeping. Redis and the PostgreSQL
        checkpoint are read INDEPENDENTLY: a transient PG failure must not
        discard the Redis summary, and a Redis loss recovers from the
        checkpoint. When the checkpoint is unreadable, ``healthy=False`` — the
        turn then skips folding entirely rather than risk a from-scratch fold
        overwriting the accumulated checkpoint or re-folding already-folded
        turns.

        CONTENT and WATERMARK always travel together (round-2 fix): the
        watermark (folded_through) lives only in the PG checkpoint, so whenever
        the checkpoint exists its CONTENT is authoritative too — pairing the
        checkpoint's watermark with a possibly-newer Redis blob would let a
        fold that reached PG but not Redis (or vice versa) skip the very lines
        the mismatched watermark claims are done. The checkpoint is self-
        consistent (both written in one upsert), so a Redis-only newer fold is
        simply re-folded next turn (idempotent, never lost). Redis is used for
        content only when no checkpoint exists yet."""
        redis_summary: memory_service.RollingSummary | None = None
        try:
            redis_summary = await self.wm.load_summary(task_id)
        except Exception as exc:
            logger.warning("runtime.wm_summary_read_failed", error=type(exc).__name__)
        checkpoint: tuple[memory_service.RollingSummary, int, int] | None = None
        healthy = True
        try:
            async with self.engine.connect() as conn:
                checkpoint = await memory_service.load_working_summary_checkpoint(conn, task_id)
        except Exception as exc:
            healthy = False
            logger.warning("runtime.wm_checkpoint_read_failed", error=type(exc).__name__)
        if checkpoint is not None:
            # The checkpoint's (summary, fold_count, folded_through) is one
            # atomic, mutually-consistent unit — use all three together.
            if redis_summary != checkpoint[0]:
                try:
                    await self.wm.store_summary(task_id, checkpoint[0])  # reconcile Redis
                except Exception as exc:
                    logger.warning("runtime.wm_rewarm_failed", error=type(exc).__name__)
            return WorkingState(checkpoint[0], checkpoint[1], checkpoint[2], healthy)
        # No checkpoint yet: Redis content (if any), watermark at zero. A
        # degraded checkpoint read (healthy=False) also lands here and blocks
        # folding, so a from-scratch summary can never clobber a real one.
        summary = redis_summary if redis_summary is not None else memory_service.RollingSummary()
        return WorkingState(summary, 0, 0, healthy)

    async def fold(
        self,
        task_id: uuid.UUID,
        state: WorkingState,
        new_lines: Sequence[str],
        *,
        folded_through: int,
    ) -> memory_service.RollingSummary:
        """§8.2 mechanism 1: fold the NOT-YET-FOLDED evicted lines (the caller
        slices past the watermark) via the utility-tier model, then persist to
        Redis + the PostgreSQL checkpoint (every fold — stronger than the ≤5
        bound; the upsert is fold_count-monotonic so a stale writer can never
        regress it). Any failure still returns the folded summary — evicted
        turns are compressed, never lost silently."""
        folded = await self.folder.fold(state.summary, new_lines)
        try:
            await self.wm.store_summary(task_id, folded)
        except Exception as exc:
            logger.warning("runtime.wm_summary_store_failed", error=type(exc).__name__)
        try:
            async with self.engine.begin() as conn:
                await memory_service.checkpoint_working_summary(
                    conn,
                    task_id,
                    folded,
                    fold_count=state.fold_count + 1,
                    folded_through=folded_through,
                )
        except Exception as exc:
            logger.warning("runtime.wm_checkpoint_write_failed", error=type(exc).__name__)
        return folded

    # -- write-back (§13 steps 9–13) --------------------------------------------

    def derive_events(
        self,
        *,
        project_id: uuid.UUID,
        agent_id: str,
        task_id: uuid.UUID,
        messages: Sequence[Any],
        message_ids: Sequence[uuid.UUID],
    ) -> list[dict[str, Any]]:
        """Map the turn's typed messages to Memory Events (§13 step 11):
        DecisionLog/ConflictResolution → episodic, MeetingSummary → semantic.
        Always PROJECT scope — agents never write org records (TITAN-BRN-024).
        Each event carries a DETERMINISTIC ``wm_client_key`` derived from its
        emitted message id, so the at-least-once flush (and the crash-replay
        re-derivation) dedup instead of double-writing."""
        events: list[dict[str, Any]] = []
        for message, message_id in zip(messages, message_ids, strict=True):
            if message.type in _EPISODIC_TYPES:
                kind = "episodic"
            elif message.type in _SEMANTIC_TYPES:
                kind = "semantic"
            else:
                continue
            body = json.dumps(message.payload, ensure_ascii=False)[:_EVENT_BODY_CHARS]
            event = memory_service.MemoryEvent(
                kind=kind,
                scope=f"project:{project_id}",
                title=f"{message.type} from agent.{agent_id}"[:1000],
                body_md=body or "{}",
                metadata={
                    "message_type": message.type,
                    "wm_client_key": str(uuid.uuid5(WM_EVENT_NAMESPACE, str(message_id))),
                },
                importance=0.6 if kind == "episodic" else 0.5,
                source_task_id=task_id,
            )
            events.append(event.model_dump(mode="json"))
        return events

    async def finish_turn(
        self,
        *,
        project_id: uuid.UUID,
        agent_id: str,
        task_id: uuid.UUID,
        call_seq: int,
        note: str | None,
        messages: Sequence[Any],
        message_ids: Sequence[uuid.UUID],
        entry_id: str = "",
    ) -> None:
        """Steps 9–13, after the turn's messages committed: append the scratch
        entry, queue + flush the derived Memory Events, emit KG edge events.
        Metadata-only logging throughout (TITAN-OPS-016)."""
        try:
            await self.wm.append_scratch(
                task_id,
                call_seq,
                {
                    "seq": call_seq,
                    "agent": f"agent.{agent_id}",
                    "note": (note or "")[:2000],
                    "message_types": [m.type for m in messages],
                },
                entry_id=entry_id,
            )
        except Exception as exc:
            logger.warning("runtime.wm_scratch_failed", error=type(exc).__name__)
        try:
            events = self.derive_events(
                project_id=project_id,
                agent_id=agent_id,
                task_id=task_id,
                messages=messages,
                message_ids=message_ids,
            )
            await self.wm.queue_events(task_id, events)
        except Exception as exc:
            logger.warning("runtime.wm_queue_failed", error=type(exc).__name__)
        await self.flush_pending(task_id, agent_id=agent_id)
        try:
            kg_events = [
                KgEdgeEvent(
                    kind="decision",
                    label=m.type,
                    project_id=project_id,
                    task_id=task_id,
                    source_agent=f"agent.{agent_id}",
                )
                for m in messages
                if m.type in _EPISODIC_TYPES
            ]
            if kg_events:
                await self.kg.emit(kg_events)
        except Exception as exc:
            logger.warning("runtime.kg_sink_failed", error=type(exc).__name__)

    async def replay_write_back(
        self,
        *,
        project_id: uuid.UUID,
        agent_id: str,
        task_id: uuid.UUID,
        emitted_ids: Sequence[uuid.UUID],
    ) -> None:
        """The crash-window healer for a REPLAYED turn (emission committed, but
        the prior attempt may have died before queueing/flushing its events):
        re-derive the events from the already-committed messages and flush.
        The deterministic wm_client_keys make this idempotent — if the prior
        attempt DID flush, the dedup skips every event."""
        try:
            from titan.messaging.service import KIND_TO_TYPE, get_message  # noqa: PLC0415

            reconstructed: list[Any] = []
            ids: list[uuid.UUID] = []
            async with self.engine.connect() as conn:
                for message_id in emitted_ids:
                    row = await get_message(conn, message_id)
                    if row is None:
                        continue
                    message_type = KIND_TO_TYPE.get(row.kind)
                    if message_type is None:
                        continue
                    reconstructed.append(_ReplayMessage(type=message_type, payload=row.body))
                    ids.append(message_id)
            events = self.derive_events(
                project_id=project_id,
                agent_id=agent_id,
                task_id=task_id,
                messages=reconstructed,
                message_ids=ids,
            )
            await self.wm.queue_events(task_id, events)
        except Exception as exc:
            logger.warning("runtime.wm_replay_derive_failed", error=type(exc).__name__)
        await self.flush_pending(task_id, agent_id=agent_id)

    async def flush_pending(self, task_id: uuid.UUID, *, agent_id: str) -> int:
        """Drain `wm:{task_id}:events` through the transactional write path
        (record + chunks + outbox + ledger — §5.1), then ack exactly the
        drained snapshot. Serialised per task by the flush lock (concurrent
        flushers would double-write and mis-trim); JSON- or schema-invalid
        entries are dead-lettered to ``:events:dead`` so poison cannot block
        the TITAN-BRN-002 gate forever OR vanish silently; already-durable
        client keys are skipped (at-least-once → exactly-once effect). Any
        failure leaves the queue intact for a later flush. Returns how many
        events became durable THIS call."""
        token = await self.wm.acquire_flush_lock(task_id)
        if token is None:
            return 0  # another flusher owns the queue; it will drain it
        try:
            raws = await self.wm.raw_events(task_id)
            if not raws:
                return 0
            parsed: list[Any] = []
            poison: list[str] = []
            for raw in raws:
                try:
                    parsed.append(memory_service.MemoryEvent.model_validate(json.loads(raw)))
                except Exception:
                    # Collect, but DON'T quarantine yet: dead-lettering happens
                    # on the acked (success) path only, so a later write failure
                    # can't re-dead-letter the same entry on every retry.
                    poison.append(raw)
            written = 0
            if parsed:
                keys = [
                    str(e.metadata.get("wm_client_key", ""))
                    for e in parsed
                    if e.metadata.get("wm_client_key")
                ]
                async with self.engine.begin() as conn:
                    known = await memory_service.existing_wm_client_keys(conn, keys)
                    # Dedup against BOTH what is already durable AND repeats
                    # WITHIN this batch (a replay re-queues byte-equal events
                    # with the same client key; without intra-batch dedup both
                    # copies would insert in one transaction — the index is not
                    # unique, so it is no backstop). Keyless events (old-deploy
                    # residue) have no identity, so they pass through as-is.
                    seen: set[str] = set()
                    to_write: list[Any] = []
                    for event in parsed:
                        key = str(event.metadata.get("wm_client_key", ""))
                        if key and (key in known or key in seen):
                            continue
                        if key:
                            seen.add(key)
                        to_write.append(event)
                    if to_write:
                        await memory_service.write_events(
                            conn,
                            memory_service.MemoryEventBatch(
                                source_agent=f"agent.{agent_id}",
                                task_id=task_id,
                                events=to_write,
                            ),
                            source_agent=f"agent.{agent_id}",
                            actor_agent=f"agent.{agent_id}",
                        )
                    written = len(to_write)
            # The write committed (or there was nothing to write): NOW quarantine
            # the poison and ack the drained snapshot together, so both survive
            # a crash only as a unit and neither repeats on retry.
            for raw in poison:
                logger.warning("runtime.wm_event_dead_lettered", task_id=str(task_id))
                await self.wm.dead_letter_event(task_id, raw)
            await self.wm.ack_events(task_id, len(raws))
            return written
        except Exception as exc:
            logger.warning("runtime.wm_flush_failed", error=type(exc).__name__)
            return 0
        finally:
            try:
                await self.wm.release_flush_lock(task_id, token)
            except Exception as exc:
                logger.warning("runtime.wm_lock_release_failed", error=type(exc).__name__)


__all__ = [
    "Citation",
    "KgEdgeEvent",
    "KgEventSink",
    "MemoryPacket",
    "MemoryPacketSections",
    "MemoryProvider",
    "MemoryRequest",
    "NoopKgSink",
    "NullMemoryProvider",
    "PacketMemoryProvider",
    "WorkingMemoryCoordinator",
]
