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
