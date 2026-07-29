"""audit_events persistence model (Phase 73; Vol 8 §10, SEC-090).

The immutable, hash-chained platform audit ledger. Owned by the leaf
``titan.audit`` module; imports only ``titan.db`` (infrastructure). No FKs — an
audit row must outlive the entity it names, and a failed-auth ``actor`` may be a
non-existent account (enumeration probes). Immutability is enforced by the
DB trigger from migration 0037, not by the ORM.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, Identity, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class AuditEventRow(ConventionsMixin, Base):
    """One SEC-090 audit event. ``id`` is the event_id, ``created_at`` is ``ts``.
    The block-mutation trigger keeps every row (incl. updated_at/version) frozen."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    org: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    source_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    event_hash: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = ["AuditEventRow"]
