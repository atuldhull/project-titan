"""Events tables: outbox (Vol 3 §6) + event_inbox (consumer idempotency ledger).

The events module is a library (Vol 3 §3) — outbox is the transactional
outbox (TITAN-BCK-005); event_inbox is the inbox pattern the demo consumer
uses so replaying a stream produces no duplicate side effects (TITAN-BCK-010).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class Outbox(ConventionsMixin, Base):
    __tablename__ = "outbox"
    __table_args__ = (
        Index(
            "idx_outbox_unpublished",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class EventInbox(ConventionsMixin, Base):
    """Idempotency ledger keyed on event_id ALONE: the demo consumer inserts
    ON CONFLICT DO NOTHING, so re-delivery OR replaying the whole stream into
    a fresh consumer group produces no duplicate side effect in Postgres
    (TITAN-BCK-010 acceptance). consumer_group records who processed it first."""

    __tablename__ = "event_inbox"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    consumer_group: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
