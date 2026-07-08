"""The memory-core tables in the ``brain`` schema (Vol 5 §4.3, Phase 33).

PostgreSQL is the canonical, auditable store; Qdrant is a derived index
(ADR-05-01). These tables follow §4.3 verbatim and deliberately do NOT carry the
ConventionsMixin — §4.3 fixes their columns exactly, and the ledger/outbox are
append-only. Cross-schema FKs point into the public ``projects``/``tasks`` tables
(the spec's ``core.*``).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    Computed,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL, TIMESTAMP, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base

MEMORY_KINDS: tuple[str, ...] = ("episodic", "semantic", "procedural")
MEMORY_STATUSES: tuple[str, ...] = ("active", "archived", "superseded", "deleted")
LEDGER_ACTIONS: tuple[str, ...] = ("read", "write", "promote", "prune", "supersede")
INGEST_OPS: tuple[str, ...] = ("upsert", "delete")


class MemoryRecord(Base):
    """The canonical record of one long-term memory (§4.3)."""

    __tablename__ = "memory_records"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('episodic','semantic','procedural')", name="ck_memory_records_kind"
        ),
        CheckConstraint(
            "status IN ('active','archived','superseded','deleted')",
            name="ck_memory_records_status",
        ),
        Index("ix_memory_records_tsv", "tsv", postgresql_using="gin"),
        Index("ix_memory_records_scope_kind_status", "scope", "kind", "status"),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)  # 'org' | 'project:<uuid>'
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    source_agent: Mapped[str] = mapped_column(Text, nullable=False)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    record_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    importance: Mapped[float] = mapped_column(REAL, nullable=False, server_default=text("0.5"))
    decay_score: Mapped[float] = mapped_column(REAL, nullable=False, server_default=text("1.0"))
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.memory_records.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR(),
        Computed("to_tsvector('english', title || ' ' || body_md)", persisted=True),
        nullable=True,
    )


class MemoryChunk(Base):
    """One embeddable unit; ``id`` doubles as the Qdrant point id (ADR-05-01)."""

    __tablename__ = "memory_chunks"
    __table_args__ = (
        UniqueConstraint("record_id", "seq", name="uq_memory_chunks_record_id_seq"),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.memory_records.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embed_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class IngestOutbox(Base):
    """The §5.1 embedding-pipeline outbox: one row per chunk upsert/delete."""

    __tablename__ = "ingest_outbox"
    __table_args__ = (
        CheckConstraint("op IN ('upsert','delete')", name="ck_ingest_outbox_op"),
        Index(
            "ix_ingest_outbox_unprocessed",
            "id",
            postgresql_where=text("processed_at IS NULL"),
        ),
        {"schema": "brain"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.memory_chunks.id"), nullable=False
    )
    op: Mapped[str] = mapped_column(Text, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class MemoryLedger(Base):
    """The append-only access/audit trail: one row per memory operation
    (TITAN-BRN-005). A DB trigger rejects UPDATE/DELETE."""

    __tablename__ = "memory_ledger"
    __table_args__ = (
        CheckConstraint(
            "action IN ('read','write','promote','prune','supersede')",
            name="ck_memory_ledger_action",
        ),
        Index("ix_memory_ledger_records", "record_ids", postgresql_using="gin"),
        {"schema": "brain"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    actor_agent: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    record_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
