"""Private data access for the clarification engine (``clarification_batches`` +
``clarification_questions``). SQLAlchemy Core over an AsyncConnection; only
``service`` is importable across the module boundary."""

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7

_BATCH_COLS = (
    "id, project_id, status, timeout_hours, deadline_at, resolved_at, resolution, "
    "assumptions, opened_by, created_at, updated_at, version"
)
_QUESTION_COLS = (
    "id, batch_id, project_id, ordinal, topic, question, answer, answered_at, "
    "created_at, updated_at, version"
)


async def insert_batch(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    timeout_hours: int,
    assumptions: list[dict[str, Any]],
    opened_by: str,
) -> Row[Any]:
    """Open the project's single clarification batch. ``deadline_at`` is computed in
    the database (now() + timeout) so the window is anchored to server time."""
    result = await conn.execute(
        text(
            "INSERT INTO clarification_batches "
            "(id, project_id, timeout_hours, deadline_at, assumptions, opened_by) "
            "VALUES (:id, :pid, :timeout, now() + make_interval(hours => :timeout), "
            "CAST(:assumptions AS jsonb), :by) "
            f"RETURNING {_BATCH_COLS}"
        ),
        {
            "id": uuid7(),
            "pid": project_id,
            "timeout": timeout_hours,
            "assumptions": json.dumps(assumptions),
            "by": opened_by,
        },
    )
    return result.one()


async def insert_question(
    conn: AsyncConnection,
    *,
    batch_id: uuid.UUID,
    project_id: uuid.UUID,
    ordinal: int,
    topic: str,
    question: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            "INSERT INTO clarification_questions "
            "(id, batch_id, project_id, ordinal, topic, question) "
            "VALUES (:id, :bid, :pid, :ordinal, :topic, :question) "
            f"RETURNING {_QUESTION_COLS}"
        ),
        {
            "id": uuid7(),
            "bid": batch_id,
            "pid": project_id,
            "ordinal": ordinal,
            "topic": topic,
            "question": question,
        },
    )
    return result.one()


async def get_batch_for_update(conn: AsyncConnection, project_id: uuid.UUID) -> Row[Any] | None:
    """The project's batch, row-locked (FOR UPDATE) so concurrent answers/timeout
    resolution serialize per project."""
    result = await conn.execute(
        text(f"SELECT {_BATCH_COLS} FROM clarification_batches WHERE project_id = :pid FOR UPDATE"),
        {"pid": project_id},
    )
    return result.one_or_none()


async def get_batch(conn: AsyncConnection, project_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_BATCH_COLS} FROM clarification_batches WHERE project_id = :pid"),
        {"pid": project_id},
    )
    return result.one_or_none()


async def questions_for_batch(conn: AsyncConnection, batch_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(
            f"SELECT {_QUESTION_COLS} FROM clarification_questions "
            "WHERE batch_id = :bid ORDER BY ordinal ASC"
        ),
        {"bid": batch_id},
    )
    return result.all()


async def answer_question(
    conn: AsyncConnection, *, batch_id: uuid.UUID, question_id: uuid.UUID, answer: str
) -> Row[Any] | None:
    """Record an answer to one question. Returns None if the id does not belong to
    the batch (guards against cross-batch id spoofing)."""
    result = await conn.execute(
        text(
            "UPDATE clarification_questions SET answer = :answer, answered_at = now() "
            "WHERE id = :qid AND batch_id = :bid "
            f"RETURNING {_QUESTION_COLS}"
        ),
        {"answer": answer, "qid": question_id, "bid": batch_id},
    )
    return result.one_or_none()


async def unanswered_count(conn: AsyncConnection, batch_id: uuid.UUID) -> int:
    result = await conn.execute(
        text(
            "SELECT count(*) FROM clarification_questions WHERE batch_id = :bid AND answer IS NULL"
        ),
        {"bid": batch_id},
    )
    return int(result.scalar_one())


async def set_batch_resolution(
    conn: AsyncConnection,
    *,
    batch_id: uuid.UUID,
    status: str,
    resolution: str,
    assumptions: list[dict[str, Any]],
) -> Row[Any]:
    result = await conn.execute(
        text(
            "UPDATE clarification_batches "
            "SET status = :status, resolution = :resolution, resolved_at = now(), "
            "assumptions = CAST(:assumptions AS jsonb), version = version + 1 "
            "WHERE id = :id "
            f"RETURNING {_BATCH_COLS}"
        ),
        {
            "id": batch_id,
            "status": status,
            "resolution": resolution,
            "assumptions": json.dumps(assumptions),
        },
    )
    return result.one()


async def overdue_open_batches(conn: AsyncConnection) -> Sequence[Row[Any]]:
    """Every still-open batch past its non-blocking deadline (TITAN-WFL-008)."""
    result = await conn.execute(
        text(
            f"SELECT {_BATCH_COLS} FROM clarification_batches "
            "WHERE status = 'open' AND deadline_at < now() ORDER BY deadline_at ASC"
        )
    )
    return result.all()


__all__ = [
    "answer_question",
    "get_batch",
    "get_batch_for_update",
    "insert_batch",
    "insert_question",
    "overdue_open_batches",
    "questions_for_batch",
    "set_batch_resolution",
    "unanswered_count",
]
