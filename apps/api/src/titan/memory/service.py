"""Public service interface of the `memory` module (TITAN-BCK-004).

Responsibility: the Memory Service write path — Memory Events, the canonical
record/chunk store, the transactional embedding outbox, the append-only audit
ledger (Vol 5 §4, §5.1, TITAN-BRN-001/005) — and, from Phase 35, the embedding
ingestion pipeline (outbox relay → ``titan:memory:ingest`` → gateway
/embeddings → Qdrant) plus the ``rebuild-index`` reconstruction command
(TITAN-BRN-004). Retrieval (§6) lands in Phase 36.

Only this interface may be imported by other modules; the ORM models, the
repository, the chunker, and the Qdrant/ingest internals stay private to the
package. Pydantic v2 DTOs — never SQLAlchemy models — cross this boundary.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7
from titan.gateway.dto import JsonbDict
from titan.gateway.problems import ProblemException, problem_type
from titan.memory import repository as repo
from titan.memory.chunker import (
    CONTENT_TYPES,
    ChunkContext,
    ContentType,
    count_tokens,
    get_chunker,
    token_cost,
)
from titan.memory.ingest import (
    EMBEDDERS_GROUP,
    INGEST_DLQ,
    STREAM_MEMORY_INGEST,
    EmbeddingsClient,
    IngestRelay,
    MemoryEmbedder,
    run_embedder,
    run_ingest_reaper,
    run_ingest_relay,
)
from titan.memory.packets import (
    PACKET_CACHE_TTL_SECONDS,
    AssembledFor,
    MemoryPacketOut,
    PacketCitation,
    PacketRequest,
    PacketSections,
    assemble_packet,
    build_reranker,
    cheapest_chat_alias,
    packet_cache_key,
)
from titan.memory.qdrant import COLLECTION_SPECS, QdrantIndex
from titan.memory.rebuild import CollectionRebuild, RebuildReport, rebuild_index
from titan.memory.retrieval import (
    CrossEncoderReranker,
    LlmListwiseReranker,
    RankedChunk,
    Reranker,
    RetrievalQuery,
    retrieve,
    shape_queries,
)
from titan.memory.working import (
    WM_TTL_SECONDS,
    RollingSummary,
    SummaryFolder,
    WorkingMemory,
    pending_wm_events,
)

MemoryKind = Literal["episodic", "semantic", "procedural"]
LedgerAction = Literal["read", "write", "promote", "prune", "supersede"]

# Upper bound on a single memory body (chars). Distilled records are small; the
# cap keeps the generated tsvector well under Postgres's ~1 MiB limit so an
# oversized body is rejected at validation (422), never a 500 mid-write.
MAX_BODY_MD_CHARS = 200_000


def parse_scope(scope: str) -> uuid.UUID | None:
    """The project id a scope names, or None for org scope (§3.2). Raises
    ValueError on a malformed scope — 'org' | 'project:<uuid>' are the only
    shapes (surfaces as a 422 from the Pydantic validator)."""
    if scope == "org":
        return None
    if scope.startswith("project:"):
        try:
            return uuid.UUID(scope[len("project:") :])
        except ValueError as exc:
            raise ValueError("project scope must be 'project:<uuid>'") from exc
    raise ValueError("scope must be 'org' or 'project:<uuid>'")


# --- DTOs ------------------------------------------------------------------


class MemoryEvent(BaseModel):
    """One durable memory to write (§5.1). ``kind`` and ``scope`` are mandatory
    (TITAN-BRN-001); ``content_type`` selects the chunker (§5.2)."""

    model_config = ConfigDict(extra="forbid")

    kind: MemoryKind
    scope: str = Field(min_length=1)
    content_type: ContentType = "markdown"
    title: str = Field(min_length=1, max_length=1000)
    # A memory record is DISTILLED knowledge, not a blob (large docs are the KB's
    # job, §10, and are chunked there). The cap keeps the STORED generated tsvector
    # comfortably under Postgres's ~1 MiB tsvector limit, so an oversized body is a
    # clean 422 at the boundary rather than a 54000 error surfacing as a 500.
    body_md: str = Field(min_length=1, max_length=MAX_BODY_MD_CHARS)
    metadata: dict[str, Any] = Field(default_factory=dict)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    # Optional per-event task provenance (else the batch's task_id is used).
    source_task_id: uuid.UUID | None = None

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        parse_scope(value)  # raises ValueError on a malformed scope -> 422
        return value

    @field_validator("content_type")
    @classmethod
    def _validate_content_type(cls, value: str) -> str:
        if value not in CONTENT_TYPES:
            raise ValueError(f"content_type must be one of {CONTENT_TYPES}")
        return value


class MemoryEventBatch(BaseModel):
    """A transactional batch of Memory Events written by one agent turn."""

    model_config = ConfigDict(extra="forbid")

    source_agent: str = Field(min_length=1, max_length=200)
    task_id: uuid.UUID | None = None
    events: list[MemoryEvent] = Field(min_length=1)


class WrittenRecord(BaseModel):
    record_id: uuid.UUID
    chunk_count: int


class MemoryWriteResult(BaseModel):
    records: list[WrittenRecord]


class MemoryRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    scope: str
    project_id: uuid.UUID | None
    source_agent: str
    source_task_id: uuid.UUID | None
    title: str
    body_md: str
    metadata: JsonbDict
    importance: float
    decay_score: float
    access_count: int
    last_accessed_at: datetime | None
    status: str
    superseded_by: uuid.UUID | None
    created_at: datetime


class MemoryChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    record_id: uuid.UUID
    seq: int
    text: str
    token_count: int
    embed_model: str | None
    embedded_at: datetime | None


class MemoryLedgerEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    actor_agent: str
    task_id: uuid.UUID | None
    action: str
    record_ids: list[uuid.UUID]
    detail: JsonbDict


class MemoryRecordDetail(BaseModel):
    """A record + its chunks + its complete ledger trail (§14 GET endpoint)."""

    record: MemoryRecordOut
    chunks: list[MemoryChunkOut]
    ledger: list[MemoryLedgerEntry]


def _validation_error(detail: str) -> ProblemException:
    return ProblemException(
        status=422, title="Invalid request", detail=detail, problem_type=problem_type("validation")
    )


# --- write path (§5.1) -----------------------------------------------------


async def write_events(
    conn: AsyncConnection,
    batch: MemoryEventBatch,
    *,
    source_agent: str,
    actor_agent: str,
) -> MemoryWriteResult:
    """Write each event's record + chunks + ingest-outbox rows, plus one ``write``
    ledger row per event, ALL in the caller's single transaction (§5.1,
    TITAN-BRN-005). A failure anywhere (a bad project FK, a CHECK violation)
    aborts the whole batch — no partial writes. The caller owns the transaction
    (``engine.begin()``), so raising here rolls everything back.

    ``source_agent`` is application data — WHICH agent role authored the memory,
    stored on the record. ``actor_agent`` is the AUTHENTICATED principal that
    performed the write, stamped on the append-only ledger: the audit trail's
    actor is never spoofable client input (TITAN-BRN-005)."""
    written: list[WrittenRecord] = []
    try:
        for event in batch.events:
            # TITAN-BRN-024: agents never write org-scope records directly —
            # org promotion is the curation workflow's multi-signal decision.
            # Enforced here (the in-process runtime write-back path) and again
            # at the HTTP surface, so no caller shape can slip an org write
            # through on agent authority.
            if actor_agent.startswith("agent.") and event.scope == "org":
                raise ProblemException(
                    status=403,
                    title="Forbidden",
                    detail=(
                        "Agent credentials cannot write org-scope memory records "
                        "(TITAN-BRN-024); write project scope and let curation promote."
                    ),
                    problem_type=problem_type("forbidden"),
                )
            record_id = uuid7()
            await repo.insert_record(
                conn,
                record_id=record_id,
                kind=event.kind,
                scope=event.scope,
                project_id=parse_scope(event.scope),
                source_agent=source_agent,
                source_task_id=event.source_task_id or batch.task_id,
                title=event.title,
                body_md=event.body_md,
                metadata=json.dumps(event.metadata),
                importance=event.importance,
            )
            ctx = ChunkContext(scope=event.scope, kind=event.kind, title=event.title)
            chunks = get_chunker(event.content_type).chunk(event.body_md, ctx)
            for chunk in chunks:
                chunk_id = uuid7()
                await repo.insert_chunk(
                    conn,
                    chunk_id=chunk_id,
                    record_id=record_id,
                    seq=chunk.seq,
                    chunk_text=chunk.text,
                    token_count=chunk.token_count,
                )
                await repo.insert_outbox(conn, chunk_id=chunk_id, op="upsert")
            # TITAN-BRN-005: exactly one ledger row per write operation, attributed
            # to the authenticated actor (not the record's declared source_agent).
            await repo.append_ledger(
                conn,
                actor_agent=actor_agent,
                task_id=batch.task_id,
                action="write",
                record_ids=[record_id],
                detail=json.dumps(
                    {
                        "kind": event.kind,
                        "scope": event.scope,
                        "chunks": len(chunks),
                        "source_agent": source_agent,
                    }
                ),
            )
            written.append(WrittenRecord(record_id=record_id, chunk_count=len(chunks)))
    except IntegrityError as exc:
        # A project-scoped event whose project (or source_task) does not exist, or
        # a value the DB CHECKs reject — client error, and the whole batch rolls back.
        raise _validation_error(
            "An event references an unknown project or task, or a rejected value."
        ) from exc
    return MemoryWriteResult(records=written)


# --- read path (§14) -------------------------------------------------------


async def get_record_detail(
    conn: AsyncConnection,
    record_id: uuid.UUID,
    *,
    actor_agent: str,
    task_id: uuid.UUID | None,
) -> MemoryRecordDetail | None:
    """Fetch the canonical record + its chunks + its complete ledger trail (§14).
    A read is a first-class memory operation: it bumps the record's access
    lifecycle metadata and appends one ``read`` ledger row (TITAN-BRN-001/005).
    Runs in the caller's transaction so the read + its audit commit atomically."""
    row = await repo.get_record(conn, record_id)
    if row is None:
        return None
    await repo.touch_record(conn, record_id)
    await repo.append_ledger(
        conn,
        actor_agent=actor_agent,
        task_id=task_id,
        action="read",
        record_ids=[record_id],
        detail=json.dumps({}),
    )
    # Re-read so the returned record reflects the just-applied access bump, and the
    # ledger trail includes this very read (the complete trail, §14).
    row = await repo.get_record(conn, record_id)
    assert row is not None  # noqa: S101 — same transaction; the row cannot vanish
    chunks = await repo.list_chunks(conn, record_id)
    ledger = await repo.ledger_for_record(conn, record_id)
    return MemoryRecordDetail(
        record=MemoryRecordOut.model_validate(row),
        chunks=[MemoryChunkOut.model_validate(c) for c in chunks],
        ledger=[MemoryLedgerEntry.model_validate(entry) for entry in ledger],
    )


# --- §14 GET /memory/metrics -------------------------------------------------


class CacheMetrics(BaseModel):
    hits: float
    misses: float
    hit_rate: float


class LagMetrics(BaseModel):
    count: float
    p50_seconds: float | None
    p95_seconds: float | None


class MemoryMetricsOut(BaseModel):
    """The §14 metrics summary: what an operator needs at a glance, computed
    from the SAME Prometheus series /metrics exports (single source of truth).
    Counts and seconds only — never memory content (TITAN-OPS-016)."""

    packet_cache: CacheMetrics
    ingest_lag: LagMetrics


def memory_metrics() -> MemoryMetricsOut:
    from prometheus_client import REGISTRY  # noqa: PLC0415 — read-side only

    def sample(name: str, labels: dict[str, str] | None = None) -> float:
        return REGISTRY.get_sample_value(name, labels or {}) or 0.0

    hits = sample("titan_memory_packet_cache_total", {"result": "hit"})
    misses = sample("titan_memory_packet_cache_total", {"result": "miss"})
    total = hits + misses

    # Histogram quantiles from cumulative buckets (linear interpolation — the
    # standard histogram_quantile estimate, adequate for an operator summary).
    buckets: list[tuple[float, float]] = []
    for metric in REGISTRY.collect():
        if metric.name == "titan_memory_ingest_lag_seconds":
            for s in metric.samples:
                if s.name.endswith("_bucket"):
                    upper = float("inf") if s.labels["le"] == "+Inf" else float(s.labels["le"])
                    buckets.append((upper, s.value))
    buckets.sort(key=lambda b: b[0])
    count = buckets[-1][1] if buckets else 0.0

    def quantile(q: float) -> float | None:
        if not buckets or count == 0:
            return None
        target = q * count
        previous_upper, previous_count = 0.0, 0.0
        for upper, cumulative in buckets:
            if cumulative >= target:
                if upper == float("inf"):
                    return previous_upper
                span = cumulative - previous_count
                if span <= 0:
                    return upper
                return previous_upper + (upper - previous_upper) * (
                    (target - previous_count) / span
                )
            previous_upper, previous_count = upper, cumulative
        return previous_upper

    return MemoryMetricsOut(
        packet_cache=CacheMetrics(
            hits=hits, misses=misses, hit_rate=(hits / total) if total else 0.0
        ),
        ingest_lag=LagMetrics(count=count, p50_seconds=quantile(0.5), p95_seconds=quantile(0.95)),
    )


# --- working-memory checkpoints (§8.2, TITAN-BRN-016) ------------------------


async def checkpoint_working_summary(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    summary: RollingSummary,
    *,
    fold_count: int,
    folded_through: int,
) -> None:
    """Persist one fold's result to ``brain.working_summaries`` — the bound on
    what a Redis loss can cost (TITAN-BRN-002/-016). Monotonic on fold_count:
    a stale or from-scratch writer can never regress the checkpoint."""
    await repo.upsert_working_summary(
        conn,
        task_id,
        summary=summary.model_dump_json(),
        fold_count=fold_count,
        folded_through=folded_through,
    )


async def load_working_summary_checkpoint(
    conn: AsyncConnection, task_id: uuid.UUID
) -> tuple[RollingSummary, int, int] | None:
    """The checkpointed (summary, fold_count, folded_through), or None. A
    schema-skewed stored blob (old deploy) counts as absent rather than
    crashing hydration."""
    row = await repo.load_working_summary(conn, task_id)
    if row is None:
        return None
    try:
        raw = row.summary if isinstance(row.summary, dict) else json.loads(row.summary)
        return RollingSummary.model_validate(raw), int(row.fold_count), int(row.folded_through)
    except Exception:
        return None


async def existing_wm_client_keys(conn: AsyncConnection, keys: list[str]) -> set[str]:
    """Which write-back client keys are already durable (flush dedup)."""
    return await repo.existing_wm_client_keys(conn, keys)


__all__ = [
    "AssembledFor",
    "COLLECTION_SPECS",
    "CacheMetrics",
    "CollectionRebuild",
    "ContentType",
    "CrossEncoderReranker",
    "EMBEDDERS_GROUP",
    "EmbeddingsClient",
    "INGEST_DLQ",
    "IngestRelay",
    "LagMetrics",
    "LedgerAction",
    "LlmListwiseReranker",
    "MemoryChunkOut",
    "MemoryEmbedder",
    "MemoryEvent",
    "MemoryEventBatch",
    "MemoryKind",
    "MemoryLedgerEntry",
    "MemoryMetricsOut",
    "MemoryPacketOut",
    "MemoryRecordDetail",
    "MemoryRecordOut",
    "MemoryWriteResult",
    "PACKET_CACHE_TTL_SECONDS",
    "PacketCitation",
    "PacketRequest",
    "PacketSections",
    "QdrantIndex",
    "RankedChunk",
    "RebuildReport",
    "Reranker",
    "RetrievalQuery",
    "RollingSummary",
    "STREAM_MEMORY_INGEST",
    "SummaryFolder",
    "WM_TTL_SECONDS",
    "WorkingMemory",
    "WrittenRecord",
    "assemble_packet",
    "build_reranker",
    "cheapest_chat_alias",
    "checkpoint_working_summary",
    "count_tokens",
    "existing_wm_client_keys",
    "get_record_detail",
    "load_working_summary_checkpoint",
    "memory_metrics",
    "packet_cache_key",
    "parse_scope",
    "pending_wm_events",
    "rebuild_index",
    "retrieve",
    "run_embedder",
    "run_ingest_reaper",
    "run_ingest_relay",
    "shape_queries",
    "token_cost",
    "write_events",
]
