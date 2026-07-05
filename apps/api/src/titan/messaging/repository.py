"""Private data access for the `messaging` module (messages, decisions,
notifications). SQLAlchemy Core over an AsyncConnection; only ``service.py``
crosses the module boundary (TITAN-BCK-004)."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.gateway.pagination import Keyset

_MESSAGE_COLS = (
    "id, project_id, thread_id, kind, sender_agent_id, sender_user_id, "
    "body, task_id, created_at, version"
)
_DECISION_COLS = (
    "id, project_id, title, context, decision, alternatives, consequences, "
    "status, decided_by_agent_id, supersedes_id, created_at, version"
)
_NOTIFICATION_COLS = (
    "id, user_id, category, severity, title, body, link, read_at, created_at, version"
)


# --- messages --------------------------------------------------------------


async def insert_message(
    conn: AsyncConnection,
    *,
    message_id: uuid.UUID,
    project_id: uuid.UUID,
    thread_id: uuid.UUID,
    kind: str,
    sender_agent_id: uuid.UUID | None,
    sender_user_id: uuid.UUID | None,
    body: str,
    task_id: uuid.UUID | None,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO messages (id, project_id, thread_id, kind, sender_agent_id, "
            f"sender_user_id, body, task_id) VALUES (:id, :project_id, :thread_id, :kind, "
            f":sender_agent_id, :sender_user_id, CAST(:body AS jsonb), :task_id) "
            f"RETURNING {_MESSAGE_COLS}"
        ),
        {
            "id": message_id,
            "project_id": project_id,
            "thread_id": thread_id,
            "kind": kind,
            "sender_agent_id": sender_agent_id,
            "sender_user_id": sender_user_id,
            "body": body,
            "task_id": task_id,
        },
    )
    return result.one()


async def list_messages(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    keyset: Keyset,
    thread_id: uuid.UUID | None,
    kind: str | None,
) -> Sequence[Row[Any]]:
    clauses = ["project_id = :pid"]
    params: dict[str, Any] = {"pid": project_id, "limit": keyset.fetch}
    if thread_id is not None:
        clauses.append("thread_id = :thread_id")
        params["thread_id"] = thread_id
    if kind is not None:
        clauses.append("kind = :kind")
        params["kind"] = kind
    seek, seek_params = keyset.where()
    if seek:
        clauses.append(seek)
        params.update(seek_params)
    result = await conn.execute(
        text(
            f"SELECT {_MESSAGE_COLS} FROM messages WHERE {' AND '.join(clauses)} "
            f"ORDER BY {keyset.order_by} LIMIT :limit"
        ),
        params,
    )
    return result.all()


# --- decisions -------------------------------------------------------------


async def insert_decision(
    conn: AsyncConnection,
    *,
    decision_id: uuid.UUID,
    project_id: uuid.UUID,
    title: str,
    context: str,
    decision: str,
    alternatives: str,
    consequences: str,
    status: str,
    decided_by_agent_id: uuid.UUID | None,
    supersedes_id: uuid.UUID | None,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO decisions (id, project_id, title, context, decision, alternatives, "
            f"consequences, status, decided_by_agent_id, supersedes_id) VALUES (:id, :project_id, "
            f":title, :context, :decision, :alternatives, :consequences, :status, "
            f":decided_by_agent_id, :supersedes_id) RETURNING {_DECISION_COLS}"
        ),
        {
            "id": decision_id,
            "project_id": project_id,
            "title": title,
            "context": context,
            "decision": decision,
            "alternatives": alternatives,
            "consequences": consequences,
            "status": status,
            "decided_by_agent_id": decided_by_agent_id,
            "supersedes_id": supersedes_id,
        },
    )
    return result.one()


async def get_decision(conn: AsyncConnection, decision_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_DECISION_COLS} FROM decisions WHERE id = :id"), {"id": decision_id}
    )
    return result.one_or_none()


async def mark_decision_superseded(conn: AsyncConnection, decision_id: uuid.UUID) -> None:
    await conn.execute(
        text(
            "UPDATE decisions SET status = 'superseded', version = version + 1 "
            "WHERE id = :id AND status <> 'superseded'"
        ),
        {"id": decision_id},
    )


async def list_decisions(
    conn: AsyncConnection, *, project_id: uuid.UUID, keyset: Keyset
) -> Sequence[Row[Any]]:
    seek, seek_params = keyset.where()
    extra = f"AND {seek}" if seek else ""
    result = await conn.execute(
        text(
            f"SELECT {_DECISION_COLS} FROM decisions WHERE project_id = :pid {extra} "
            f"ORDER BY {keyset.order_by} LIMIT :limit"
        ),
        {"pid": project_id, "limit": keyset.fetch, **seek_params},
    )
    return result.all()


# --- notifications ---------------------------------------------------------


async def insert_notification(
    conn: AsyncConnection,
    *,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
    category: str,
    severity: str,
    title: str,
    body: str | None,
    link: str | None,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO notifications (id, user_id, category, severity, title, body, link) "
            f"VALUES (:id, :user_id, :category, :severity, :title, :body, :link) "
            f"RETURNING {_NOTIFICATION_COLS}"
        ),
        {
            "id": notification_id,
            "user_id": user_id,
            "category": category,
            "severity": severity,
            "title": title,
            "body": body,
            "link": link,
        },
    )
    return result.one()


async def get_notification(conn: AsyncConnection, notification_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_NOTIFICATION_COLS} FROM notifications WHERE id = :id"),
        {"id": notification_id},
    )
    return result.one_or_none()


async def list_notifications(
    conn: AsyncConnection, *, user_id: uuid.UUID, keyset: Keyset, unread_only: bool
) -> Sequence[Row[Any]]:
    clauses = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": keyset.fetch}
    if unread_only:
        clauses.append("read_at IS NULL")
    seek, seek_params = keyset.where()
    if seek:
        clauses.append(seek)
        params.update(seek_params)
    result = await conn.execute(
        text(
            f"SELECT {_NOTIFICATION_COLS} FROM notifications WHERE {' AND '.join(clauses)} "
            f"ORDER BY {keyset.order_by} LIMIT :limit"
        ),
        params,
    )
    return result.all()


async def mark_notification_read(
    conn: AsyncConnection, notification_id: uuid.UUID, user_id: uuid.UUID
) -> Row[Any] | None:
    """Set read_at for the caller's own notification. Returns the row (idempotent
    — a re-read keeps the original read_at), or None if it is missing or not the
    caller's (the router maps None to 404, never leaking another user's rows)."""
    result = await conn.execute(
        text(
            f"UPDATE notifications SET read_at = COALESCE(read_at, now()), version = version + 1 "
            f"WHERE id = :id AND user_id = :uid RETURNING {_NOTIFICATION_COLS}"
        ),
        {"id": notification_id, "uid": user_id},
    )
    return result.one_or_none()


async def mark_all_notifications_read(conn: AsyncConnection, user_id: uuid.UUID) -> int:
    result = await conn.execute(
        text(
            "UPDATE notifications SET read_at = now(), version = version + 1 "
            "WHERE user_id = :uid AND read_at IS NULL"
        ),
        {"uid": user_id},
    )
    return result.rowcount
