"""Communication tables: messages, decisions, notifications (Vol 3 §6, Vol 4 §7)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class Message(ConventionsMixin, Base):
    # This IS the spec's ``agent_messages`` store (Vol 2 §6.3): the Phase-6 table
    # is named ``messages`` and Phase 27 extends it with the full §6.1 envelope
    # (schema_version/correlation_id/from_agent/to_agent/priority/requires_ack)
    # below — one message table for both agent-bus traffic and human/REST posts.
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "sender_agent_id IS NOT NULL OR sender_user_id IS NOT NULL OR from_agent IS NOT NULL",
            name="ck_messages_sender_present",
        ),
        Index("idx_messages_thread", "thread_id", "created_at"),
        Index("idx_messages_project_kind", "project_id", "kind", text("created_at DESC")),
        Index(
            "idx_messages_correlation",
            "correlation_id",
            postgresql_where=text("correlation_id IS NOT NULL"),
        ),
        Index(
            "idx_messages_to_agent",
            "project_id",
            "to_agent",
            "created_at",
            postgresql_where=text("to_agent IS NOT NULL"),
        ),
        # The task Sheet's ?task_id filter (migration 0030).
        Index(
            "idx_messages_project_task",
            "project_id",
            "task_id",
            postgresql_where=text("task_id IS NOT NULL"),
        ),
        # One agent's own output, for the §6.5 detail drawer (migration 0031). A
        # human's messages carry sender_user_id instead, so they never match —
        # and note this only ever holds actor-driven TRANSITION messages, since
        # `bus.send` sets sender_agent_id=None and identifies the sender by role.
        Index(
            "idx_messages_project_sender_agent",
            "project_id",
            "sender_agent_id",
            postgresql_where=text("sender_agent_id IS NOT NULL"),
        ),
        # Where an agent's REAL output is found: the bus records the sender as a
        # role here (migration 0031). Mirrors idx_messages_to_agent above.
        Index(
            "idx_messages_project_from_agent",
            "project_id",
            "from_agent",
            text("created_at DESC"),
            postgresql_where=text("from_agent IS NOT NULL"),
        ),
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
    # --- §6.1 AgentMessage envelope (Phase 27) ---
    schema_version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'1.0'"))
    # request/response pairing across the bus (§6.2 gatekeeper replies reuse it)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # role ids (e.g. "pm_project"/"eng_backend"), not runtime agent-row uuids
    from_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # critical|high|normal|low
    priority: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'normal'"))
    requires_ack: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))


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


class NotificationPreference(ConventionsMixin, Base):
    """Per-user, per-category channel routing (Vol 4 §7.1, Phase 64).

    A row exists ONLY where a user has overridden the §7.1 default, so the
    absence of a row is meaningful: it means "track the default". Materialising
    defaults for every user would freeze them at signup time, and a later change
    to a default would then reach new users only.

    ``channels`` is a JSONB array rather than a join table: the value set is
    four fixed strings that change with the spec, not with data, and the whole
    matrix is always read and written as one document.
    """

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "category", name="uq_notification_preferences_user_category"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # approval|failure|progress|activity|security (Vol 4 §7.1)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    # Subset of toast|bell|attention_queue|plugins.
    channels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
