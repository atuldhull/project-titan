"""Private data access for the `memory` module (the ``brain`` schema). SQLAlchemy
Core over an AsyncConnection; only ``service.py`` crosses the module boundary
(TITAN-BCK-004)."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

_RECORD_COLS = (
    "id, kind, scope, project_id, source_agent, source_task_id, title, body_md, "
    "metadata, importance, decay_score, access_count, last_accessed_at, status, "
    "superseded_by, created_at"
)
_CHUNK_COLS = "id, record_id, seq, text, token_count, embed_model, embedded_at"
_LEDGER_COLS = "id, at, actor_agent, task_id, action, record_ids, detail"


# --- records ---------------------------------------------------------------


async def insert_record(
    conn: AsyncConnection,
    *,
    record_id: uuid.UUID,
    kind: str,
    scope: str,
    project_id: uuid.UUID | None,
    source_agent: str,
    source_task_id: uuid.UUID | None,
    title: str,
    body_md: str,
    metadata: str,
    importance: float,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO brain.memory_records "
            f"(id, kind, scope, project_id, source_agent, source_task_id, title, body_md, "
            f"metadata, importance) VALUES (:id, :kind, :scope, :project_id, :source_agent, "
            f":source_task_id, :title, :body_md, CAST(:metadata AS jsonb), :importance) "
            f"RETURNING {_RECORD_COLS}"
        ),
        {
            "id": record_id,
            "kind": kind,
            "scope": scope,
            "project_id": project_id,
            "source_agent": source_agent,
            "source_task_id": source_task_id,
            "title": title,
            "body_md": body_md,
            "metadata": metadata,
            "importance": importance,
        },
    )
    return result.one()


async def get_record(conn: AsyncConnection, record_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_RECORD_COLS} FROM brain.memory_records WHERE id = :id"),
        {"id": record_id},
    )
    return result.one_or_none()


async def touch_record(conn: AsyncConnection, record_id: uuid.UUID) -> None:
    """Bump a record's access lifecycle metadata on read (TITAN-BRN-001)."""
    await conn.execute(
        text(
            "UPDATE brain.memory_records SET access_count = access_count + 1, "
            "last_accessed_at = now() WHERE id = :id"
        ),
        {"id": record_id},
    )


# --- chunks ----------------------------------------------------------------


async def insert_chunk(
    conn: AsyncConnection,
    *,
    chunk_id: uuid.UUID,
    record_id: uuid.UUID,
    seq: int,
    chunk_text: str,
    token_count: int,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO brain.memory_chunks (id, record_id, seq, text, token_count) "
            f"VALUES (:id, :record_id, :seq, :text, :token_count) RETURNING {_CHUNK_COLS}"
        ),
        {
            "id": chunk_id,
            "record_id": record_id,
            "seq": seq,
            "text": chunk_text,
            "token_count": token_count,
        },
    )
    return result.one()


async def list_chunks(conn: AsyncConnection, record_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(f"SELECT {_CHUNK_COLS} FROM brain.memory_chunks WHERE record_id = :rid ORDER BY seq"),
        {"rid": record_id},
    )
    return result.all()


# --- ingest outbox ---------------------------------------------------------


async def insert_outbox(conn: AsyncConnection, *, chunk_id: uuid.UUID, op: str) -> None:
    await conn.execute(
        text("INSERT INTO brain.ingest_outbox (chunk_id, op) VALUES (:chunk_id, :op)"),
        {"chunk_id": chunk_id, "op": op},
    )


async def drainable_outbox(
    conn: AsyncConnection, *, republish_before_seconds: float, batch: int
) -> Sequence[Row[Any]]:
    """The relay's working set: unprocessed rows never published, PLUS rows
    published longer than ``republish_before_seconds`` ago but still unprocessed
    (heals a wiped/lossy Redis — PostgreSQL is canonical, ADR-05-01). Dead rows
    (quarantined after MAX_ATTEMPTS deliveries) are EXCLUDED — republishing them
    would defeat the DLQ (§5.1); an operator replay clears ``dead_at``. Row-
    locked with SKIP LOCKED so concurrent relays never double-publish."""
    result = await conn.execute(
        text(
            "SELECT id, chunk_id, op, enqueued_at FROM brain.ingest_outbox "
            "WHERE processed_at IS NULL AND dead_at IS NULL AND (published_at IS NULL "
            "  OR published_at < now() - make_interval(secs => :stale)) "
            "ORDER BY id LIMIT :batch FOR UPDATE SKIP LOCKED"
        ),
        {"stale": republish_before_seconds, "batch": batch},
    )
    return result.all()


async def mark_outbox_dead(conn: AsyncConnection, outbox_id: int) -> None:
    """Terminal quarantine (§5.1): the row's message was dead-lettered; the relay
    must stop republishing it. Cleared only by an explicit operator replay."""
    await conn.execute(
        text("UPDATE brain.ingest_outbox SET dead_at = now() WHERE id = :id"),
        {"id": outbox_id},
    )


async def mark_outbox_published(conn: AsyncConnection, outbox_id: int) -> None:
    await conn.execute(
        text("UPDATE brain.ingest_outbox SET published_at = now() WHERE id = :id"),
        {"id": outbox_id},
    )


async def outbox_row(conn: AsyncConnection, outbox_id: int) -> Row[Any] | None:
    result = await conn.execute(
        text(
            "SELECT id, chunk_id, op, enqueued_at, published_at, processed_at "
            "FROM brain.ingest_outbox WHERE id = :id"
        ),
        {"id": outbox_id},
    )
    return result.one_or_none()


async def chunk_for_embedding(conn: AsyncConnection, chunk_id: uuid.UUID) -> Row[Any] | None:
    """A chunk joined to its record — everything the embedder needs: the text to
    embed and the §4.4 payload fields."""
    result = await conn.execute(
        text(
            "SELECT c.id AS chunk_id, c.record_id, c.seq, c.text, "
            "       r.scope, r.kind, r.project_id, r.source_agent, r.created_at, "
            "       r.decay_score, r.status, r.metadata "
            "FROM brain.memory_chunks c JOIN brain.memory_records r ON r.id = c.record_id "
            "WHERE c.id = :cid"
        ),
        {"cid": chunk_id},
    )
    return result.one_or_none()


async def mark_embedded(
    conn: AsyncConnection, *, chunk_id: uuid.UUID, outbox_id: int, embed_model: str
) -> None:
    """Stamp the §5.1 success markers in ONE transaction: the chunk's
    embedded_at/embed_model and the outbox row's processed_at."""
    await conn.execute(
        text(
            "UPDATE brain.memory_chunks SET embedded_at = now(), embed_model = :model "
            "WHERE id = :cid"
        ),
        {"cid": chunk_id, "model": embed_model},
    )
    await conn.execute(
        text("UPDATE brain.ingest_outbox SET processed_at = now() WHERE id = :id"),
        {"id": outbox_id},
    )


async def mark_outbox_processed(conn: AsyncConnection, outbox_id: int) -> None:
    await conn.execute(
        text("UPDATE brain.ingest_outbox SET processed_at = now() WHERE id = :id"),
        {"id": outbox_id},
    )


async def later_delete_exists(
    conn: AsyncConnection, *, chunk_id: uuid.UUID, after_outbox_id: int
) -> bool:
    """True when a ``delete`` op for this chunk was enqueued AFTER the given
    outbox row. The outbox (canonical, ordered) arbitrates op order — parallel
    embedders may interleave stream deliveries, and an upsert processed after a
    later delete would resurrect a deleted point."""
    result = await conn.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM brain.ingest_outbox "
            "WHERE chunk_id = :cid AND op = 'delete' AND id > :oid)"
        ),
        {"cid": chunk_id, "oid": after_outbox_id},
    )
    return bool(result.scalar_one())


async def mark_chunks_embedded(
    conn: AsyncConnection, chunk_ids: Sequence[uuid.UUID], *, embed_model: str
) -> None:
    """Restamp a rebuild batch's chunks as embedded (TITAN-BRN-004)."""
    if not chunk_ids:
        return
    await conn.execute(
        text(
            "UPDATE brain.memory_chunks SET embedded_at = now(), embed_model = :model "
            "WHERE id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": list(chunk_ids), "model": embed_model},
    )


async def all_chunk_ids(conn: AsyncConnection) -> set[uuid.UUID]:
    """The CANONICAL chunk-id set for TITAN-BRN-004 reconciliation: chunks of
    ACTIVE records only. Non-active records must have no points — the live
    pipeline deletes them on supersede (Phase 35) and §9.2 archiving drops
    vectors — so a rebuild that re-created them would diverge from the
    live-converged index; their leftover points are strays to delete."""
    result = await conn.execute(
        text(
            "SELECT c.id FROM brain.memory_chunks c "
            "JOIN brain.memory_records r ON r.id = c.record_id "
            "WHERE r.status = 'active'"
        )
    )
    return {row.id for row in result.all()}


async def quarantined_chunk_ids(conn: AsyncConnection) -> set[uuid.UUID]:
    """Chunks whose pipeline work was dead-lettered (a live ``dead_at`` outbox
    row): known poison. Rebuild skips them — retrying deterministic failures
    would abort the TITAN-BRN-004 recovery command; an operator replay clears
    the quarantine first."""
    result = await conn.execute(
        text(
            "SELECT DISTINCT chunk_id FROM brain.ingest_outbox "
            "WHERE dead_at IS NOT NULL AND processed_at IS NULL"
        )
    )
    return {row.chunk_id for row in result.all()}


async def chunks_for_rebuild(
    conn: AsyncConnection, chunk_ids: Sequence[uuid.UUID]
) -> Sequence[Row[Any]]:
    """Chunk+record rows for a rebuild batch (same shape as chunk_for_embedding).
    Re-asserts status='active': a record superseded between the rebuild's
    snapshot and this batch must not get its points re-created (the live
    pipeline just deleted them)."""
    if not chunk_ids:
        return []
    result = await conn.execute(
        text(
            "SELECT c.id AS chunk_id, c.record_id, c.seq, c.text, "
            "       r.scope, r.kind, r.project_id, r.source_agent, r.created_at, "
            "       r.decay_score, r.status, r.metadata "
            "FROM brain.memory_chunks c JOIN brain.memory_records r ON r.id = c.record_id "
            "WHERE c.id = ANY(CAST(:ids AS uuid[])) AND r.status = 'active' ORDER BY c.id"
        ),
        {"ids": list(chunk_ids)},
    )
    return result.all()


# --- retrieval (§6.1) --------------------------------------------------------


async def lexical_search(
    conn: AsyncConnection,
    *,
    query: str,
    scopes: Sequence[str],
    kinds: Sequence[str] | None,
    limit: int,
) -> Sequence[Row[Any]]:
    """The lexical leg (§6.1, ADR-05-03): PostgreSQL FTS over the STORED tsv via
    websearch_to_tsquery + ts_rank_cd — catches exact identifiers (error codes,
    function names) that dense vectors miss. Scope-filtered in SQL: org + the
    caller's project only, never other projects (TITAN-BRN-003)."""
    kind_clause = "AND r.kind = ANY(:kinds) " if kinds else ""
    result = await conn.execute(
        text(
            "SELECT r.id AS record_id, ts_rank_cd(r.tsv, q) AS rank "
            "FROM brain.memory_records r, websearch_to_tsquery('english', :query) q "
            "WHERE r.tsv @@ q AND r.status = 'active' AND r.scope = ANY(:scopes) "
            f"{kind_clause}"
            "ORDER BY rank DESC, r.id LIMIT :limit"
        ),
        {"query": query, "scopes": list(scopes), "kinds": list(kinds or []), "limit": limit},
    )
    return result.all()


async def hydrate_chunks(
    conn: AsyncConnection, chunk_ids: Sequence[uuid.UUID], *, scopes: Sequence[str]
) -> Sequence[Row[Any]]:
    """Canonical text + ranking fields for candidate chunks (PostgreSQL is the
    source of truth; Qdrant only nominated ids — ADR-05-01). Re-asserts scope
    (defense in depth vs a stale/poisoned index — TITAN-BRN-003) and drops
    non-active records (§6.1 status filter)."""
    if not chunk_ids:
        return []
    result = await conn.execute(
        text(
            "SELECT c.id AS chunk_id, c.record_id, c.seq, c.text, c.token_count, "
            "       r.kind, r.scope, r.title, r.importance, r.decay_score, r.created_at "
            "FROM brain.memory_chunks c JOIN brain.memory_records r ON r.id = c.record_id "
            "WHERE c.id = ANY(CAST(:ids AS uuid[])) AND r.status = 'active' "
            "  AND r.scope = ANY(:scopes)"
        ),
        {"ids": list(chunk_ids), "scopes": list(scopes)},
    )
    return result.all()


async def first_chunk_ids(
    conn: AsyncConnection, record_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """record_id -> its seq-0 chunk id, for lexical-only candidates (the lexical
    leg ranks records; fusion needs a representative chunk)."""
    if not record_ids:
        return {}
    result = await conn.execute(
        text(
            "SELECT record_id, id AS chunk_id FROM brain.memory_chunks "
            "WHERE record_id = ANY(CAST(:ids AS uuid[])) AND seq = 0"
        ),
        {"ids": list(record_ids)},
    )
    return {row.record_id: row.chunk_id for row in result.all()}


async def project_identity(conn: AsyncConnection, project_id: uuid.UUID) -> Row[Any] | None:
    """The project one-pager fields for the packet's non-evictable
    identity_context (§6.2 rule c)."""
    result = await conn.execute(
        text("SELECT name, display_name, brief FROM projects WHERE id = :id"),
        {"id": project_id},
    )
    return result.one_or_none()


# --- ledger (append-only) --------------------------------------------------


async def append_ledger(
    conn: AsyncConnection,
    *,
    actor_agent: str,
    task_id: uuid.UUID | None,
    action: str,
    record_ids: list[uuid.UUID],
    detail: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO brain.memory_ledger (actor_agent, task_id, action, record_ids, detail) "
            f"VALUES (:actor_agent, :task_id, :action, :record_ids, CAST(:detail AS jsonb)) "
            f"RETURNING {_LEDGER_COLS}"
        ),
        {
            "actor_agent": actor_agent,
            "task_id": task_id,
            "action": action,
            "record_ids": record_ids,
            "detail": detail,
        },
    )
    return result.one()


async def ledger_for_record(conn: AsyncConnection, record_id: uuid.UUID) -> Sequence[Row[Any]]:
    """The complete audit trail touching ``record_id``, oldest first — the array
    membership match uses the GIN index on record_ids."""
    result = await conn.execute(
        text(
            f"SELECT {_LEDGER_COLS} FROM brain.memory_ledger "
            f"WHERE :rid = ANY(record_ids) ORDER BY at, id"
        ),
        {"rid": record_id},
    )
    return result.all()


async def upsert_working_summary(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    *,
    summary: str,
    fold_count: int,
    folded_through: int,
) -> None:
    """Checkpoint one task's rolling summary (TITAN-BRN-016). MONOTONIC: a
    stale writer (a turn that hydrated before a concurrent fold, or one that
    could not read the checkpoint and started from scratch) can never REGRESS
    the accumulated checkpoint — updates apply only at equal-or-higher
    fold_count."""
    await conn.execute(
        text(
            "INSERT INTO brain.working_summaries "
            "(task_id, summary, fold_count, folded_through, updated_at) "
            "VALUES (:tid, CAST(:summary AS jsonb), :folds, :through, now()) "
            "ON CONFLICT (task_id) DO UPDATE SET summary = EXCLUDED.summary, "
            "fold_count = EXCLUDED.fold_count, folded_through = EXCLUDED.folded_through, "
            "updated_at = now() "
            "WHERE EXCLUDED.fold_count >= brain.working_summaries.fold_count"
        ),
        {"tid": task_id, "summary": summary, "folds": fold_count, "through": folded_through},
    )


async def load_working_summary(conn: AsyncConnection, task_id: uuid.UUID) -> Row[Any] | None:
    """The checkpointed summary (summary jsonb, fold_count, folded_through) —
    the recovery path when Redis lost `wm:{task_id}:summary`."""
    result = await conn.execute(
        text(
            "SELECT summary, fold_count, folded_through "
            "FROM brain.working_summaries WHERE task_id = :tid"
        ),
        {"tid": task_id},
    )
    return result.one_or_none()


async def existing_wm_client_keys(conn: AsyncConnection, keys: Sequence[str]) -> set[str]:
    """Which write-back client keys are ALREADY durable — the flush's
    at-least-once dedup. The ``metadata ? 'wm_client_key'`` predicate is
    REQUIRED for the planner to use the PARTIAL index
    ix_memory_records_wm_client_key: Postgres cannot prove the ``->>`` equality
    implies the ``?`` key-exists predicate, so without it the lookup seq-scans
    the ever-growing records table on the hot per-turn flush path."""
    if not keys:
        return set()
    result = await conn.execute(
        text(
            "SELECT metadata ->> 'wm_client_key' AS ck FROM brain.memory_records "
            "WHERE metadata ? 'wm_client_key' AND metadata ->> 'wm_client_key' = ANY(:keys)"
        ),
        {"keys": list(keys)},
    )
    return {row.ck for row in result.all()}
