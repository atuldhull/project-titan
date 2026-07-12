"""Clarification persistence: ``clarification_batches`` + ``clarification_questions``
(Vol 6 §2.4). Mutable workflow state (answers arrive, status transitions), so both
carry the ``set_updated_at`` trigger. Private to the module — only ``service``
crosses the boundary."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, uuid7


class ClarificationBatch(Base):
    """One clarification batch per project (TITAN-WFL-007 single-batch policy). Holds
    the logged assumptions and a non-blocking ``deadline_at`` (TITAN-WFL-008) after
    which an unanswered batch is resolved with best-judgment assumptions."""

    __tablename__ = "clarification_batches"
    __table_args__ = (UniqueConstraint("project_id", name="uq_clarification_batches_project"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # open | answered | timed_out
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    timeout_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("24"))
    deadline_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    # answered | timeout_best_judgment
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    assumptions: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    opened_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class ClarificationQuestion(Base):
    """One essential clarification question (≤5 per batch); ``answer`` is filled when
    the user responds."""

    __tablename__ = "clarification_questions"
    __table_args__ = (
        UniqueConstraint("batch_id", "ordinal", name="uq_clarification_questions_batch_ordinal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clarification_batches.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
