"""Conflict-resolution + veto tables (Vol 2 §7, Phase 31).

``conflicts`` holds each dispute's current §7 state; ``conflict_transitions`` is the
append-only audit of every move (a DB trigger rejects UPDATE/DELETE, like
``decisions``). ``merge_vetoes`` is the security-veto pipeline gate; ``ConflictThresholds``
holds the live-editable deadlock thresholds (rule 5).
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class Conflict(ConventionsMixin, Base):
    __tablename__ = "conflicts"
    __table_args__ = (Index("uq_conflicts_project_cid", "project_id", "conflict_id", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # The §6.2 ConflictResolution.conflict_id — the natural thread key.
    conflict_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # technical | scope | priority | cross_domain
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    parties: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # direct | arbitration | executive | resolved
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'direct'"))
    round: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    arbiter: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_by: Mapped[str] = mapped_column(Text, nullable=False)
    deadlock_flagged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    resolution_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("decisions.id"), nullable=True
    )


class ConflictTransition(ConventionsMixin, Base):
    """Append-only audit of one state move (DB trigger rejects UPDATE/DELETE)."""

    __tablename__ = "conflict_transitions"
    __table_args__ = (Index("idx_conflict_transitions_conflict", "conflict_row_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    conflict_row_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conflicts.id", ondelete="CASCADE"), nullable=False
    )
    from_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class MergeVeto(ConventionsMixin, Base):
    """A Security veto against an artifact — blocks merge/release (TITAN-AGT-018)."""

    __tablename__ = "merge_vetoes"
    __table_args__ = (
        Index(
            "uq_merge_vetoes_active",
            "project_id",
            "artifact_ref",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "idx_merge_vetoes_task",
            "task_id",
            postgresql_where=text("task_id IS NOT NULL AND status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    artifact_ref: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    raised_by: Mapped[str] = mapped_column(Text, nullable=False)
    veto_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # active | overridden
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    override_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("decisions.id"), nullable=True
    )


class ConflictThresholds(ConventionsMixin, Base):
    """Deadlock thresholds (rule 5), live-editable in the DB (TITAN-AGT-020).

    ``project_id`` NULL is the org-wide default row; a project row overrides it.
    """

    __tablename__ = "conflict_thresholds"
    __table_args__ = (
        Index(
            "uq_conflict_thresholds_scope",
            text("COALESCE(project_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    max_conflict_rounds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    max_task_reopens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
