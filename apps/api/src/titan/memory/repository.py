"""Private data access for the `memory` module (the ``brain`` schema). SQLAlchemy
Core over an AsyncConnection; only ``service.py`` crosses the module boundary
(TITAN-BCK-004)."""

import uuid
from collections.abc import Sequence
from typing import Any, cast

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


async def insert_outbox(
    conn: AsyncConnection, *, chunk_id: uuid.UUID, op: str, collection: str = "titan_memory"
) -> None:
    """Enqueue one §5.1 embedding op. ``collection`` names the target Qdrant
    collection so the SAME pipeline embeds KB chunks into ``titan_kb`` and memory
    chunks into ``titan_memory`` (Phase 39) — the value travels with the row all
    the way to the embedder, so a delete op still knows its collection even after
    the chunk/record is gone."""
    await conn.execute(
        text(
            "INSERT INTO brain.ingest_outbox (chunk_id, op, collection) "
            "VALUES (:chunk_id, :op, :collection)"
        ),
        {"chunk_id": chunk_id, "op": op, "collection": collection},
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
            "SELECT id, chunk_id, op, collection, enqueued_at FROM brain.ingest_outbox "
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
            "SELECT id, chunk_id, op, collection, enqueued_at, published_at, processed_at "
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


async def active_chunk_ids(conn: AsyncConnection, *, kb: bool | None = None) -> set[uuid.UUID]:
    """The CANONICAL chunk-id set for TITAN-BRN-004 reconciliation: chunks of
    ACTIVE records only. Non-active records must have no points — the live
    pipeline deletes them on supersede (Phase 35) and §9.2 archiving drops
    vectors — so a rebuild that re-created them would diverge from the
    live-converged index; their leftover points are strays to delete.

    ``kb`` partitions by target collection (Phase 39): ``kb=False`` → the
    ``titan_memory`` canonical set (records WITHOUT a ``kb_slug`` marker),
    ``kb=True`` → the ``titan_kb`` canonical set (materialised KB records),
    ``None`` → both. A KB chunk in titan_memory (or vice versa) would be a
    mis-embedded stray, so rebuild reconciles each collection against its own
    partition."""
    clause = ""
    if kb is True:
        clause = "AND r.metadata ? 'kb_slug' "
    elif kb is False:
        clause = "AND NOT (r.metadata ? 'kb_slug') "
    result = await conn.execute(
        text(
            "SELECT c.id FROM brain.memory_chunks c "
            "JOIN brain.memory_records r ON r.id = c.record_id "
            f"WHERE r.status = 'active' {clause}"
        )
    )
    return {row.id for row in result.all()}


async def supersede_record(
    conn: AsyncConnection, *, record_id: uuid.UUID, superseded_by: uuid.UUID, collection: str
) -> None:
    """Retire a record's current version (§10.2: only the approved current KB
    version stays in ``titan_kb``): mark it superseded and enqueue a delete op
    per chunk so the live pipeline drops its points from ``collection``. The
    body stays in PostgreSQL (queryable history) — only the derived vectors go."""
    await conn.execute(
        text(
            "UPDATE brain.memory_records SET status = 'superseded', superseded_by = :sb "
            "WHERE id = :id"
        ),
        {"id": record_id, "sb": superseded_by},
    )
    chunk_rows = await conn.execute(
        text("SELECT id FROM brain.memory_chunks WHERE record_id = :rid"),
        {"rid": record_id},
    )
    for (chunk_id,) in chunk_rows:
        await insert_outbox(conn, chunk_id=chunk_id, op="delete", collection=collection)


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


# --- Knowledge Base (§10.2/§10.3) --------------------------------------------

_KB_DOC_COLS = "id, doc_type, slug, title, current_version, status, tags, created_at"
_KB_VERSION_COLS = (
    "id, document_id, version, body_md, change_note, proposed_by, approved_by, "
    "record_id, created_at"
)
_KB_PROPOSAL_COLS = (
    "id, document_id, doc_type, slug, title, body_md, change_note, rationale, tags, "
    "proposed_by, status, requires_human, near_duplicates, decided_by, decided_at, "
    "resulting_version, created_at"
)


async def get_kb_document_by_slug(conn: AsyncConnection, slug: str) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_KB_DOC_COLS} FROM brain.kb_documents WHERE slug = :slug"),
        {"slug": slug},
    )
    return result.one_or_none()


async def get_kb_document(conn: AsyncConnection, document_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_KB_DOC_COLS} FROM brain.kb_documents WHERE id = :id"),
        {"id": document_id},
    )
    return result.one_or_none()


async def insert_kb_document(
    conn: AsyncConnection,
    *,
    doc_type: str,
    slug: str,
    title: str,
    tags: list[str],
) -> Row[Any]:
    result = await conn.execute(
        text(
            "INSERT INTO brain.kb_documents (doc_type, slug, title, current_version, tags) "
            "VALUES (:doc_type, :slug, :title, 1, :tags) "
            f"RETURNING {_KB_DOC_COLS}"
        ),
        {"doc_type": doc_type, "slug": slug, "title": title, "tags": tags},
    )
    return result.one()


async def bump_kb_document(
    conn: AsyncConnection,
    document_id: uuid.UUID,
    *,
    version: int,
    title: str,
    tags: list[str],
) -> None:
    """Advance the document's current pointer to a freshly-approved version."""
    await conn.execute(
        text(
            "UPDATE brain.kb_documents SET current_version = :v, title = :title, tags = :tags "
            "WHERE id = :id"
        ),
        {"id": document_id, "v": version, "title": title, "tags": tags},
    )


async def insert_kb_version(
    conn: AsyncConnection,
    *,
    document_id: uuid.UUID,
    version: int,
    body_md: str,
    change_note: str,
    proposed_by: str,
    approved_by: str,
    record_id: uuid.UUID | None,
) -> Row[Any]:
    result = await conn.execute(
        text(
            "INSERT INTO brain.kb_versions "
            "(document_id, version, body_md, change_note, proposed_by, approved_by, record_id) "
            "VALUES (:doc, :v, :body, :note, :prop, :appr, :rec) "
            f"RETURNING {_KB_VERSION_COLS}"
        ),
        {
            "doc": document_id,
            "v": version,
            "body": body_md,
            "note": change_note,
            "prop": proposed_by,
            "appr": approved_by,
            "rec": record_id,
        },
    )
    return result.one()


async def current_kb_version(conn: AsyncConnection, document_id: uuid.UUID) -> Row[Any] | None:
    """The document's current (highest-approved) version row — its ``record_id``
    is the memory_record to supersede when a new version is approved."""
    qualified = ", ".join(f"v.{col}" for col in _KB_VERSION_COLS.split(", "))
    result = await conn.execute(
        text(
            f"SELECT {qualified} FROM brain.kb_versions v "
            "JOIN brain.kb_documents d ON d.id = v.document_id "
            "WHERE v.document_id = :doc AND v.version = d.current_version"
        ),
        {"doc": document_id},
    )
    return result.one_or_none()


async def list_kb_versions(conn: AsyncConnection, document_id: uuid.UUID) -> Sequence[Row[Any]]:
    """Every version of a document, newest first — the Memory Explorer history
    (older versions remain queryable even though only the current is in
    titan_kb, §10.2)."""
    result = await conn.execute(
        text(
            f"SELECT {_KB_VERSION_COLS} FROM brain.kb_versions "
            "WHERE document_id = :doc ORDER BY version DESC"
        ),
        {"doc": document_id},
    )
    return result.all()


async def insert_kb_proposal(
    conn: AsyncConnection,
    *,
    document_id: uuid.UUID | None,
    doc_type: str,
    slug: str,
    title: str,
    body_md: str,
    change_note: str,
    rationale: str,
    tags: list[str],
    proposed_by: str,
    requires_human: bool,
    near_duplicates: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            "INSERT INTO brain.kb_proposals "
            "(document_id, doc_type, slug, title, body_md, change_note, rationale, tags, "
            " proposed_by, requires_human, near_duplicates) "
            "VALUES (:doc, :doc_type, :slug, :title, :body, :note, :rationale, :tags, "
            " :prop, :rh, CAST(:nd AS jsonb)) "
            f"RETURNING {_KB_PROPOSAL_COLS}"
        ),
        {
            "doc": document_id,
            "doc_type": doc_type,
            "slug": slug,
            "title": title,
            "body": body_md,
            "note": change_note,
            "rationale": rationale,
            "tags": tags,
            "prop": proposed_by,
            "rh": requires_human,
            "nd": near_duplicates,
        },
    )
    return result.one()


async def get_kb_proposal(conn: AsyncConnection, proposal_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_KB_PROPOSAL_COLS} FROM brain.kb_proposals WHERE id = :id"),
        {"id": proposal_id},
    )
    return result.one_or_none()


async def decide_kb_proposal(
    conn: AsyncConnection,
    proposal_id: uuid.UUID,
    *,
    status: str,
    decided_by: str,
    resulting_version: int | None,
) -> bool:
    """Record the approval/rejection decision — but ONLY on a still-``pending``
    proposal, so two concurrent approvals of the same proposal cannot both
    materialise a version. Returns True when THIS call won the transition."""
    result = await conn.execute(
        text(
            "UPDATE brain.kb_proposals SET status = :status, decided_by = :by, "
            "decided_at = now(), resulting_version = :rv "
            "WHERE id = :id AND status = 'pending'"
        ),
        {"id": proposal_id, "status": status, "by": decided_by, "rv": resulting_version},
    )
    return bool(result.rowcount)


async def kb_slugs_for_records(
    conn: AsyncConnection, record_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """record_id → its KB slug, for the near-duplicate conflict check (§10.3):
    map the records a semantic search surfaced back to their document slugs."""
    if not record_ids:
        return {}
    result = await conn.execute(
        text(
            "SELECT id, metadata ->> 'kb_slug' AS slug FROM brain.memory_records "
            "WHERE id = ANY(CAST(:ids AS uuid[])) AND metadata ? 'kb_slug'"
        ),
        {"ids": list(record_ids)},
    )
    return {row.id: row.slug for row in result.all()}


async def kb_current_chunks_for_slugs(
    conn: AsyncConnection, slugs: Sequence[str], *, scopes: Sequence[str]
) -> Sequence[Row[Any]]:
    """Chunks of the CURRENT (active) KB record for each slug — the pin-by-slug
    read (TITAN-BRN-022): deterministic, no Qdrant, scope-isolated. The
    ``metadata ? 'kb_slug'`` predicate lets the planner use the partial index
    ix_memory_records_kb_slug."""
    if not slugs:
        return []
    result = await conn.execute(
        text(
            "SELECT c.id AS chunk_id, c.record_id, c.seq, c.text, c.token_count, "
            "       r.kind, r.title, r.importance, r.decay_score, r.metadata ->> 'kb_slug' AS slug "
            "FROM brain.memory_chunks c JOIN brain.memory_records r ON r.id = c.record_id "
            "WHERE r.status = 'active' AND r.metadata ? 'kb_slug' "
            "  AND r.metadata ->> 'kb_slug' = ANY(:slugs) AND r.scope = ANY(:scopes) "
            "ORDER BY r.metadata ->> 'kb_slug', c.seq"
        ),
        {"slugs": list(slugs), "scopes": list(scopes)},
    )
    return result.all()


# --- Knowledge Graph (§7.1, ADR-05-04) ---------------------------------------

_KG_NODE_COLS = "id, node_type, scope, name, properties, record_id, created_at"


async def ensure_kg_node(
    conn: AsyncConnection,
    *,
    node_type: str,
    scope: str,
    name: str,
    properties: str,
    record_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Idempotent node upsert on the UNIQUE (scope, node_type, name) key — the
    deterministic entity dedup (importing the same name twice in a scope yields
    ONE node, TITAN-BRN-014's exact-match half). Properties MERGE (existing ||
    new, new wins) so re-imports enrich rather than clobber; ``record_id`` is set
    only when currently null (the first prose binding wins)."""
    result = await conn.execute(
        text(
            "INSERT INTO brain.kg_nodes (node_type, scope, name, properties, record_id) "
            "VALUES (:nt, :scope, :name, CAST(:props AS jsonb), :rec) "
            "ON CONFLICT (scope, node_type, name) DO UPDATE SET "
            "  properties = brain.kg_nodes.properties || EXCLUDED.properties, "
            "  record_id = COALESCE(brain.kg_nodes.record_id, EXCLUDED.record_id) "
            "RETURNING id"
        ),
        {"nt": node_type, "scope": scope, "name": name, "props": properties, "rec": record_id},
    )
    return cast(uuid.UUID, result.scalar_one())


async def get_kg_node_id(
    conn: AsyncConnection, *, scope: str, node_type: str, name: str
) -> uuid.UUID | None:
    """Resolve a node's id by its natural key (scope, node_type, name)."""
    result = await conn.execute(
        text(
            "SELECT id FROM brain.kg_nodes "
            "WHERE scope = :scope AND node_type = :nt AND name = :name"
        ),
        {"scope": scope, "nt": node_type, "name": name},
    )
    return result.scalar_one_or_none()


async def add_kg_edge(
    conn: AsyncConnection,
    *,
    src: uuid.UUID,
    dst: uuid.UUID,
    edge_type: str,
    properties: str,
) -> uuid.UUID | None:
    """Add a CURRENT edge, idempotently: a no-op when an active (valid_to IS
    NULL) edge of the same (src, dst, edge_type) already exists. Returns the new
    edge id, or None when one already existed. Deterministic — the structured
    writers can be replayed without duplicating edges.

    Idempotency is enforced by the PARTIAL UNIQUE index ux_kg_edges_active
    (src, dst, edge_type) WHERE valid_to IS NULL — safe by construction, so two
    concurrent transactions cannot both insert the same active edge (the ``ON
    CONFLICT DO NOTHING`` makes the loser a clean no-op rather than a 23505).
    History rows (valid_to set) are outside the partial index, so a supersede
    that stamps valid_to then re-adds a fresh active edge never conflicts."""
    result = await conn.execute(
        text(
            "INSERT INTO brain.kg_edges (src, dst, edge_type, properties) "
            "VALUES (:src, :dst, :et, CAST(:props AS jsonb)) "
            "ON CONFLICT (src, dst, edge_type) WHERE valid_to IS NULL DO NOTHING "
            "RETURNING id"
        ),
        {"src": src, "dst": dst, "et": edge_type, "props": properties},
    )
    return result.scalar_one_or_none()


async def expire_kg_edges_from(
    conn: AsyncConnection, *, src: uuid.UUID, edge_types: Sequence[str] | None = None
) -> int:
    """Stamp ``valid_to`` on the active edges OUT of ``src`` (a superseded
    decision keeps its edges as history, §7.3). Returns how many were expired."""
    clause = "AND edge_type = ANY(:ets) " if edge_types else ""
    result = await conn.execute(
        text(
            "UPDATE brain.kg_edges SET valid_to = now() "
            f"WHERE src = :src AND valid_to IS NULL {clause}"
        ),
        {"src": src, "ets": list(edge_types or [])},
    )
    return int(result.rowcount)


async def traverse_kg(
    conn: AsyncConnection,
    *,
    start_ids: Sequence[uuid.UUID],
    edge_types: Sequence[str] | None,
    scopes: Sequence[str],
    max_depth: int,
    direction: str,
) -> tuple[Sequence[Row[Any]], Sequence[Row[Any]]]:
    """Typed graph traversal via a recursive CTE HARD-CAPPED at ``max_depth``
    (≤3, §7.1). Follows only CURRENT edges (valid_to IS NULL) whose type is in
    ``edge_types`` (all types when None), staying inside ``scopes`` (org + the
    caller's project — TITAN-BRN-003). ``direction`` is 'out' (src→dst), 'in'
    (dst→src), or 'both' (undirected — "what is connected"). Returns
    (reached nodes with min depth, induced current edges among them)."""
    if not start_ids:
        return [], []
    if direction == "out":
        neigh, this_end = "e.src = w.id", "e.dst"
    elif direction == "in":
        neigh, this_end = "e.dst = w.id", "e.src"
    else:  # both — undirected neighbour on either end
        neigh = "(e.src = w.id OR e.dst = w.id)"
        this_end = "CASE WHEN e.src = w.id THEN e.dst ELSE e.src END"
    type_clause = "AND e.edge_type = ANY(:ets) " if edge_types else ""
    plain_type_clause = "AND edge_type = ANY(:ets) " if edge_types else ""
    # UNION (set semantics) collapses cycle re-visits; depth < max_depth caps the
    # walk. MIN(depth) per node is taken in the outer aggregation.
    nodes_result = await conn.execute(
        text(
            "WITH RECURSIVE walk(id, depth) AS ("
            "  SELECT n.id, 0 FROM brain.kg_nodes n "
            "  WHERE n.id = ANY(:starts) AND n.scope = ANY(:scopes) "
            "  UNION "
            "  SELECT nxt.id, w.depth + 1 "
            "  FROM walk w "
            f"  JOIN brain.kg_edges e ON {neigh} AND e.valid_to IS NULL {type_clause}"
            f"  JOIN brain.kg_nodes nxt ON nxt.id = ({this_end}) AND nxt.scope = ANY(:scopes) "
            "  WHERE w.depth < :maxd"
            ") "
            f"SELECT n.{', n.'.join(_KG_NODE_COLS.split(', '))}, MIN(w.depth) AS depth "
            "FROM walk w JOIN brain.kg_nodes n ON n.id = w.id "
            "GROUP BY n.id, n.node_type, n.scope, n.name, n.properties, n.record_id, n.created_at "
            "ORDER BY MIN(w.depth), n.name"
        ),
        {
            "starts": list(start_ids),
            "scopes": list(scopes),
            "maxd": max_depth,
            "ets": list(edge_types or []),
        },
    )
    nodes = nodes_result.all()
    reached = [row.id for row in nodes]
    if not reached:
        return nodes, []
    edges_result = await conn.execute(
        text(
            "SELECT id, src, dst, edge_type, properties, valid_from, valid_to "
            "FROM brain.kg_edges "
            "WHERE valid_to IS NULL AND src = ANY(:reached) AND dst = ANY(:reached) "
            f"{plain_type_clause}"
            "ORDER BY edge_type, src"
        ),
        {"reached": reached, "ets": list(edge_types or [])},
    )
    return nodes, edges_result.all()
