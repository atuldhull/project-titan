"""Transactional outbox (TITAN-BCK-005): no committed event is ever lost.

``write_event`` appends the event row in the SAME DB transaction as the state
change; a background ``OutboxDrainer`` publishes unpublished rows to the queue
and stamps ``published_at``. The outbox row id IS the event_id, so a crash
between commit and publish just re-drains the same event_id — the consumer
dedups it (TITAN-BCK-010). Postgres stays the source of truth (TITAN-FND-052).
"""

import json
import uuid
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.db import uuid7
from titan.events.bus import QueueBus

logger = structlog.get_logger("titan.events")


async def write_event(
    conn: AsyncConnection,
    topic: str,
    payload: dict[str, Any],
    *,
    actor: str,
    trace_id: str | None = None,
    event_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Append an event to the outbox inside the caller's transaction.

    Returns the event_id (the outbox row id). Commits with the caller's
    transaction — if the transaction rolls back, the event is never sent. Pass
    ``event_id`` to make the outbox row id (hence the published event_id, hence
    the consumer's dedup key) a caller-chosen id — the agent bus uses the
    message_id so delivery is idempotent per message (TITAN-AGT-016).
    """
    event_id = event_id or uuid7()
    await conn.execute(
        text(
            "INSERT INTO outbox (id, topic, payload) VALUES (:id, :topic, CAST(:payload AS jsonb))"
        ),
        {
            "id": event_id,
            "topic": topic,
            "payload": _wrap(actor, trace_id, payload),
        },
    )
    return event_id


class OutboxDrainer:
    """Publishes unpublished outbox rows to the queue, idempotently."""

    def __init__(self, engine: AsyncEngine, bus: QueueBus, *, batch: int = 100) -> None:
        self._engine = engine
        self._bus = bus
        self._batch = batch

    async def drain_once(self) -> int:
        """Publish one batch of unpublished rows; returns how many. FOR UPDATE
        SKIP LOCKED lets multiple drainers run without double-publishing."""
        published = 0
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, topic, payload, created_at FROM outbox "
                        "WHERE published_at IS NULL ORDER BY created_at "
                        "LIMIT :batch FOR UPDATE SKIP LOCKED"
                    ),
                    {"batch": self._batch},
                )
            ).all()
            for row in rows:
                # asyncpg may return jsonb as str or dict depending on codec.
                wrapper = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
                await self._bus.publish(
                    row.topic,
                    wrapper.get("data", {}),
                    actor=wrapper.get("actor", "system"),
                    trace_id=wrapper.get("trace_id"),
                    event_id=str(row.id),
                    occurred_at=row.created_at,
                )
                await conn.execute(
                    text("UPDATE outbox SET published_at = now() WHERE id = :id"),
                    {"id": row.id},
                )
                published += 1
        if published:
            logger.info("outbox.drained", count=published)
        return published


def _wrap(actor: str, trace_id: str | None, payload: dict[str, Any]) -> str:
    return json.dumps({"actor": actor, "trace_id": trace_id, "data": payload})
