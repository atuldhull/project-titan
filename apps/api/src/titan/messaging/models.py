"""Communication tables: messages, decisions, notifications (Vol 3 §6, Vol 4 §7)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class Message(ConventionsMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "sender_agent_id IS NOT NULL OR sender_user_id IS NOT NULL",
            name="sender_present",
        ),
        Index("idx_messages_thread", "thread_id", "created_at"),
        Index("idx_messages_project_kind", "project_id", "kind", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # UUIDv7; groups a conversation
    thread_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # task_assignment|progress_report|review_request|architecture_discussion|
    # bug_report|decision_log|meeting_summary|conflict_resolution|human
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    sender_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id"), nullable=True
    )
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # structured per-kind schema (Vol 2 §6 protocol)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)


class Decision(ConventionsMixin, Base):
    __tablename__ = "decisions"
    __table_args__ = (Index("idx_decisions_project", "project_id", text("created_at DESC")),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    alternatives: Mapped[str] = mapped_column(Text, nullable=False)
    consequences: Mapped[str] = mapped_column(Text, nullable=False)
    # proposed|accepted|superseded
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'accepted'"))
    decided_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id"), nullable=True
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("decisions.id"), nullable=True
    )


class Notification(ConventionsMixin, Base):
    """Per-user persisted notifications (TITAN-FRT-038; the 15th table).

    The partial index on user_id WHERE read_at IS NULL keeps the bell badge
    count cheap across reloads.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index(
            "idx_notifications_unread",
            "user_id",
            postgresql_where=text("read_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # approval|failure|progress|activity|security (Vol 4 §7.1)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    # critical|high|info|low
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # in-app deep link, e.g. /projects/netflix-clone/board?task=…
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
