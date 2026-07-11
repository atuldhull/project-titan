"""The §5.1 embedding-ingestion pipeline (Vol 5; ADR-05-01, TITAN-BRN-006/007).

    ingest_outbox --relay (1 s poll)--> titan:memory:ingest --group "embedders"-->
    gateway /embeddings --> Qdrant upsert (point id == chunk id, wait=true) -->
    stamp chunk.embedded_at + outbox.processed_at --> XACK

Built ON the Phase-10 machinery (RedisStreamBus: XADD → XREADGROUP/XACK,
XAUTOCLAIM reaping, delivery-count DLQ routing) — this module adds no second
bus. Guarantees:

* **At-least-once, exactly-once effect.** The relay publishes under a STABLE
  event id (``mi-{outbox_id}``); a crash between publish and the published_at
  stamp just republishes the same id, which consumers dedup after first ack and
  otherwise resolve idempotently (Qdrant upsert by chunk-id + a processed_at
  fast-path). Rows published but never processed are REPUBLISHED after a stale
  window — PostgreSQL is canonical, so even a wiped Redis heals (ADR-05-01).
* **Ack after durability.** Qdrant calls use ``wait=true``; the stream delivery
  is acked only after Qdrant confirmed AND the success markers committed.
* **Crash recovery.** Unacked deliveries sit in the group's pending-entries
  list; the reaper XAUTOCLAIMs them (retry with exponential, delivery-count-
  scaled backoff) and routes genuine poison to the DLQ after ``MAX_ATTEMPTS``
  deliveries with a Prometheus alert gauge (bus ``_to_dlq``).
* **TITAN-BRN-006.** Embeddings go ONLY through the gateway's provider-agnostic
  ``embed`` — this module knows no provider, model, or SDK.

The ingestion-lag histogram (outbox commit → searchable) is the TITAN-BRN-007
acceptance metric (p95 ≤ 5 s at ≤ 10 events/s).
"""

import asyncio
import contextlib
import datetime as dt
import json
from typing import Any, Protocol

import structlog
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.events.service import Delivery, Envelope, QueueBus, dlq_stream
from titan.llm.service import EmbeddingsRequest, EmbeddingsResponse, RequestMetadata
from titan.memory import repository as repo
from titan.memory.qdrant import VECTOR_NAME, QdrantIndex
from titan.observability.metrics import MEMORY_INGEST_LAG, STREAM_DEPTH

logger = structlog.get_logger("titan.memory")

# Vol 5 §4.5 keyspace. The DLQ name follows the platform-wide ADR-BCK-02
# convention (`{stream}.dlq`, via dlq_stream) rather than Vol 5's ad-hoc
# `:dlq` spelling — one convention for every dead-letter stream in TITAN.
STREAM_MEMORY_INGEST = "titan:memory:ingest"
EMBEDDERS_GROUP = "embedders"

# §5.1: max 5 deliveries, then dead-letter with an alert.
MAX_ATTEMPTS = 5
# Delivery-count-scaled backoff before reprocessing a reclaimed delivery
# (exponential, bounded so a poison message cannot stall the reaper).
BACKOFF_BASE_SECONDS = 0.25
BACKOFF_CAP_SECONDS = 4.0

_ACTOR = "memory-relay"


class EmbeddingsClient(Protocol):
    """The one gateway capability this pipeline uses (TITAN-BRN-006). Satisfied
    by :class:`titan.llm.service.LlmGateway`; tests pass a deterministic stub."""

    async def embed(self, req: EmbeddingsRequest) -> EmbeddingsResponse: ...


def stable_event_id(outbox_id: int) -> str:
    """The dedup key for one outbox row — identical across republishes, so the
    consumer's seen-ledger collapses duplicates after the first ack."""
    return f"mi-{outbox_id}"


def backoff_seconds(delivery_count: int) -> float:
    """Exponential in the delivery count, capped: 0.5, 1, 2, 4, 4 …"""
    return min(BACKOFF_BASE_SECONDS * (2.0**delivery_count), BACKOFF_CAP_SECONDS)


# --- relay (outbox → stream) -------------------------------------------------


class IngestRelay:
    """Drains ``brain.ingest_outbox`` to the ingest stream (§5.1: 1 s poll).
    Two-phase like the Phase-10 drainer: publish, then stamp published_at in the
    same transaction that row-locked the batch (SKIP LOCKED keeps concurrent
    relays disjoint)."""

    def __init__(
        self,
        engine: AsyncEngine,
        bus: QueueBus,
        *,
        batch: int = 128,
        republish_after_seconds: float = 300.0,
    ) -> None:
        self._engine = engine
        self._bus = bus
        self._batch = batch
        self._republish_after = republish_after_seconds

    async def relay_once(self) -> int:
        published = 0
        async with self._engine.begin() as conn:
            rows = await repo.drainable_outbox(
                conn, republish_before_seconds=self._republish_after, batch=self._batch
            )
            for row in rows:
                await self._bus.publish(
                    STREAM_MEMORY_INGEST,
                    {
                        "outbox_id": int(row.id),
                        "chunk_id": str(row.chunk_id),
                        "op": row.op,
                        # The target collection travels WITH the row so a KB
                        # chunk lands in titan_kb and a delete still knows its
                        # collection after the chunk/record is gone (Phase 39).
                        "collection": row.collection,
                    },
                    actor=_ACTOR,
                    event_id=stable_event_id(int(row.id)),
                    occurred_at=row.enqueued_at,
                )
                await repo.mark_outbox_published(conn, int(row.id))
                published += 1
        if published:
            logger.info("memory.ingest_relayed", count=published)
        return published


# --- embedder (stream → gateway → Qdrant → stamps) ---------------------------


class MemoryEmbedder:
    """One §5.1 worker: embeds a chunk through the gateway and upserts it into
    Qdrant under its chunk id. Idempotent per delivery (processed_at fast-path +
    upsert-by-id); acks only after Qdrant confirmed and the stamps committed."""

    def __init__(
        self,
        engine: AsyncEngine,
        bus: QueueBus,
        qdrant: QdrantIndex,
        embeddings: EmbeddingsClient,
        *,
        collection: str = "titan_memory",
    ) -> None:
        self._engine = engine
        self._bus = bus
        self._qdrant = qdrant
        self._embeddings = embeddings
        self._collection = collection

    @property
    def qdrant(self) -> QdrantIndex:
        """The index this embedder writes to (the loop bootstraps collections)."""
        return self._qdrant

    @property
    def engine(self) -> AsyncEngine:
        """The canonical store (the reaper's dead-letter quarantine writes here)."""
        return self._engine

    async def handle(self, delivery: Delivery) -> bool:
        """Process one delivery; returns True when acked (success or no-op),
        False when nacked (left pending for the reaper's retry)."""
        try:
            await self._process(delivery.envelope.payload)
        except Exception:
            logger.exception(
                "memory.ingest_failed",
                event_id=delivery.envelope.event_id,
                delivery_count=delivery.delivery_count,
            )
            await self._bus.nack(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP, delivery)
            return False
        await self._bus.ack(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP, delivery)
        return True

    async def _process(self, payload: dict[str, Any]) -> None:
        # Connection hygiene: ONE read connection before the network calls, ONE
        # write transaction after — never a DB connection held across gateway or
        # Qdrant I/O, and no per-query reconnect storms on unpooled engines.
        outbox_id = int(payload["outbox_id"])
        # The chunk's target collection rides with the delivery (Phase 39); the
        # constructor default covers pre-column deliveries (legacy/replays).
        collection = str(payload.get("collection") or self._collection)
        async with self._engine.connect() as conn:
            row = await repo.outbox_row(conn, outbox_id)
            if row is None or row.processed_at is not None:
                return  # replay of already-confirmed work — idempotent no-op
            chunk = None
            superseded = False
            if row.op != "delete":
                chunk = await repo.chunk_for_embedding(conn, row.chunk_id)
                # Op-order guard: the outbox (canonical, ordered) may already
                # hold a LATER delete for this chunk; with parallel embedders
                # the stream deliveries can interleave, and embedding now would
                # resurrect a deleted point. The later delete op owns the final
                # state — complete as a no-op.
                superseded = await repo.later_delete_exists(
                    conn, chunk_id=row.chunk_id, after_outbox_id=outbox_id
                )
        if row.op == "delete":
            await self._qdrant.delete_points(collection, [str(row.chunk_id)])
            async with self._engine.begin() as conn:
                await repo.mark_outbox_processed(conn, outbox_id)
            _observe_lag(row.enqueued_at)
            return
        if superseded:
            # Defensive cleanup, not just a skip: a CRASHED earlier attempt may
            # already have upserted the point after the delete op ran (its
            # Qdrant delete no-op'd on the not-yet-existing point). Deleting
            # here makes every redelivery converge to the deleted end state.
            await self._qdrant.delete_points(collection, [str(row.chunk_id)])
            async with self._engine.begin() as conn:
                await repo.mark_outbox_processed(conn, outbox_id)
            return
        if chunk is None:
            # The record (and its chunks) vanished between enqueue and now —
            # nothing to embed; drop any stale point defensively and complete.
            await self._qdrant.delete_points(collection, [str(row.chunk_id)])
            async with self._engine.begin() as conn:
                await repo.mark_outbox_processed(conn, outbox_id)
            return
        response = await self._embeddings.embed(
            EmbeddingsRequest(
                input=[chunk.text],
                metadata=RequestMetadata(
                    agent_id="memory.ingest",
                    project_id=str(chunk.project_id) if chunk.project_id else None,
                ),
            )
        )
        point = build_point(chunk, response.embeddings[0].embedding)
        await self._qdrant.upsert_points(collection, [point])
        # Re-check INSIDE the success transaction: a delete enqueued while we
        # were embedding may have been processed concurrently (its Qdrant delete
        # could precede our upsert). Undo our own write so the end state
        # converges to deleted.
        async with self._engine.begin() as conn:
            superseded = await repo.later_delete_exists(
                conn, chunk_id=row.chunk_id, after_outbox_id=outbox_id
            )
            if not superseded:
                await repo.mark_embedded(
                    conn,
                    chunk_id=row.chunk_id,
                    outbox_id=outbox_id,
                    embed_model=response.model_id,
                )
        if superseded:
            await self._qdrant.delete_points(collection, [str(row.chunk_id)])
            async with self._engine.begin() as conn:
                await repo.mark_outbox_processed(conn, outbox_id)
            return
        _observe_lag(row.enqueued_at)


def build_point(chunk: Row[Any], vector: list[float]) -> dict[str, Any]:
    """A Qdrant point for one chunk: id == chunk id (ADR-05-01), named ``dense``
    vector, payload = exactly the §4.4 indexed fields (built from the RECORD —
    Qdrant is a derived index, canonical text stays in PostgreSQL). A KB chunk
    (its record carries a ``kb_slug``) additionally gets the §4.4 titan_kb index
    fields (doc_type, version, language) so the same point serves both the
    retrieval leg (scope/kind/record_id) and the KB collection's schema."""
    metadata = _metadata_dict(chunk.metadata)
    payload: dict[str, Any] = {
        "scope": chunk.scope,
        "kind": chunk.kind,
        "record_id": str(chunk.record_id),
        "source_agent": chunk.source_agent,
        "created_at": chunk.created_at.isoformat(),
        "tags": _tags_of(metadata),
        "decay_score": float(chunk.decay_score),
        # §6.1 filters by status; not in the §4.4 INDEX list, so payload-only.
        "status": chunk.status,
    }
    if chunk.project_id is not None:
        payload["project_id"] = str(chunk.project_id)
    if metadata.get("kb"):
        payload["doc_type"] = str(metadata.get("doc_type", ""))
        payload["version"] = str(metadata.get("kb_version", ""))
        if metadata.get("language"):
            payload["language"] = str(metadata["language"])
    return {
        "id": str(chunk.chunk_id),
        "vector": {VECTOR_NAME: vector},
        "payload": payload,
    }


def _metadata_dict(metadata: Any) -> dict[str, Any]:
    """A record's metadata JSONB as a dict — tolerant of asyncpg returning jsonb
    as str or dict, and of a non-object value."""
    data = json.loads(metadata) if isinstance(metadata, str) else metadata
    return data if isinstance(data, dict) else {}


def _tags_of(metadata: dict[str, Any]) -> list[str]:
    """The record's tags[] from its (already-parsed) metadata — tolerant of
    absence and of a non-list value."""
    tags = metadata.get("tags")
    if not isinstance(tags, list):
        return []
    return [str(tag) for tag in tags]


def _observe_lag(enqueued_at: dt.datetime) -> None:
    """Commit → searchable latency (TITAN-BRN-007's p95 ≤ 5 s histogram)."""
    lag = (dt.datetime.now(dt.UTC) - enqueued_at).total_seconds()
    MEMORY_INGEST_LAG.observe(max(lag, 0.0))


# --- batch units (unit-tested; loops below drive them) -----------------------


async def process_ingest_batch(
    embedder: MemoryEmbedder, bus: QueueBus, consumer: str, *, count: int = 16, block_ms: int = 1000
) -> int:
    """Consume one batch of NEW deliveries and handle each; returns the count."""
    deliveries = await bus.consume(
        STREAM_MEMORY_INGEST, EMBEDDERS_GROUP, consumer, count=count, block_ms=block_ms
    )
    for delivery in deliveries:
        await embedder.handle(delivery)
    return len(deliveries)


async def reap_ingest_once(
    embedder: MemoryEmbedder, bus: QueueBus, consumer: str, *, min_idle_ms: int = 30_000
) -> int:
    """Reclaim idle-pending deliveries (a crashed worker's PEL) and REPROCESS
    them with delivery-count-scaled backoff; the bus routes poison past
    ``MAX_ATTEMPTS`` to the DLQ (`{stream}.dlq`) and raises the alert gauge.

    Dead-lettering is made TERMINAL in PostgreSQL via ``on_dead``: the poisoned
    outbox row is stamped ``dead_at`` so the relay's stale-republish can never
    resurrect it (which would re-run the whole retry cycle and duplicate the
    DLQ entry forever). An operator replay clears the stamp explicitly."""

    async def quarantine(envelope: Envelope) -> None:
        raw = envelope.payload.get("outbox_id") if isinstance(envelope.payload, dict) else None
        try:
            outbox_id = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            # A foreign/malformed message has no outbox row to stamp — the DLQ
            # disposal alone is the correct terminal action for it.
            logger.warning("memory.ingest_dead_lettered_malformed", payload=str(raw))
            return
        async with embedder.engine.begin() as conn:
            await repo.mark_outbox_dead(conn, outbox_id)
        logger.warning("memory.ingest_dead_lettered", outbox_id=outbox_id)

    reclaimed = await bus.reap(
        STREAM_MEMORY_INGEST,
        EMBEDDERS_GROUP,
        consumer,
        min_idle_ms=min_idle_ms,
        max_attempts=MAX_ATTEMPTS,
        on_dead=quarantine,
    )
    for delivery in reclaimed:
        await asyncio.sleep(backoff_seconds(delivery.delivery_count))
        await embedder.handle(delivery)
    STREAM_DEPTH.labels(stream=STREAM_MEMORY_INGEST, group=EMBEDDERS_GROUP).set(
        await bus.pending_depth(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
    )
    return len(reclaimed)


# --- loops (worker process; self-healing like every Phase-10 loop) ------------


async def run_ingest_relay(  # pragma: no cover — live-verified loop shell
    relay: IngestRelay, *, stop: asyncio.Event, poll_seconds: float = 1.0
) -> None:
    """Drain the ingest outbox every ``poll_seconds`` (§5.1: 1 s) until stopped."""
    while not stop.is_set():
        try:
            published = await relay.relay_once()
        except Exception:
            logger.exception("memory.relay_loop_error")
            await _sleep_or_stop(stop, 2.0)
            continue
        if not published:
            await _sleep_or_stop(stop, poll_seconds)


async def run_embedder(  # pragma: no cover — live-verified loop shell
    embedder: MemoryEmbedder, bus: QueueBus, consumer: str, *, stop: asyncio.Event
) -> None:
    """Consume the ingest stream as group ``embedders`` until stopped. Bootstraps
    the §4.4 collections first, retrying until Qdrant is reachable — a worker
    that boots before Qdrant must not poison every delivery into the DLQ."""
    await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
    while not stop.is_set():
        try:
            await embedder.qdrant.ensure_collections()
            break
        except Exception:
            logger.exception("memory.collection_bootstrap_retry")
            await _sleep_or_stop(stop, 5.0)
    while not stop.is_set():
        try:
            processed = await process_ingest_batch(embedder, bus, consumer)
        except Exception:
            logger.exception("memory.embedder_loop_error")
            await _sleep_or_stop(stop, 2.0)
            continue
        if not processed:
            await _sleep_or_stop(stop, 0.5)


async def run_ingest_reaper(  # pragma: no cover — live-verified loop shell
    embedder: MemoryEmbedder,
    bus: QueueBus,
    consumer: str,
    *,
    stop: asyncio.Event,
    min_idle_ms: int = 30_000,
    interval_seconds: float = 15.0,
) -> None:
    """Reclaim + retry crashed deliveries; dead-letter poison (§5.1)."""
    await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
    while not stop.is_set():
        try:
            await reap_ingest_once(embedder, bus, consumer, min_idle_ms=min_idle_ms)
        except Exception:
            logger.exception("memory.ingest_reaper_loop_error")
        await _sleep_or_stop(stop, interval_seconds)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:  # pragma: no cover
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


INGEST_DLQ = dlq_stream(STREAM_MEMORY_INGEST)

__all__ = [
    "EMBEDDERS_GROUP",
    "INGEST_DLQ",
    "MAX_ATTEMPTS",
    "STREAM_MEMORY_INGEST",
    "EmbeddingsClient",
    "IngestRelay",
    "MemoryEmbedder",
    "backoff_seconds",
    "build_point",
    "process_ingest_batch",
    "reap_ingest_once",
    "run_embedder",
    "run_ingest_reaper",
    "run_ingest_relay",
    "stable_event_id",
]
