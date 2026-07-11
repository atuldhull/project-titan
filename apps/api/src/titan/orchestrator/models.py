"""Orchestration tables: tasks, task_dependencies, and the per-project delivery
pipeline (pipeline_states, pipeline_stage_events) — Vol 3 §6 / Vol 6 §2.1; owned
per §3."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class Task(ConventionsMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_project_status_priority", "project_id", "status", "priority"),
        Index(
            "idx_tasks_assignee",
            "assignee_agent_id",
            postgresql_where=text("status IN ('assigned', 'in_progress')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    sprint_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sprints.id"), nullable=True)
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("milestones.id"), nullable=True
    )
    # epics → subtasks
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # full task specification (markdown)
    spec: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # backlog|ready|assigned|in_progress|in_review|changes_requested|done|reopened|cancelled
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'backlog'"))
    # 1 critical … 5 trivial
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))
    estimate_points: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    assignee_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id"), nullable=True
    )
    reviewer_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id"), nullable=True
    )
    created_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id"), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class TaskDependency(ConventionsMixin, Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (CheckConstraint("task_id <> depends_on_id", name="no_self_dependency"),)

    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )


class PipelineState(ConventionsMixin, Base):
    """The per-project 12-stage delivery-pipeline cursor (TITAN-WFL-001). The
    (current_stage, iteration) pair is the resumable state; ``project_id`` is the
    PK so there is exactly one pipeline per project."""

    __tablename__ = "pipeline_states"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    # One of the 12 titan.orchestrator.pipeline.STAGES keys.
    current_stage: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ceo_vision'")
    )
    # Times the current stage has been (re)entered — the §2.5/§2.2 retry counter.
    iteration: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    # running | needs_human | released
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    # TITAN-WFL-005 per-project gate retry budget (Phase 42; default 3).
    retry_budget: Mapped[int] = mapped_column(nullable=False, server_default=text("3"))


class PipelineStageEvent(Base):
    """Append-only stage ledger: one row per pipeline transition (TITAN-WFL-001).
    The durable event log that reconstructs the full history; an UPDATE/DELETE is
    rejected by a DB trigger (TITAN-AGT-019)."""

    __tablename__ = "pipeline_stage_events"
    __table_args__ = (Index("ix_pipeline_stage_events_project_id", "project_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    # enter | stage_complete | rejected | not_applicable | needs_human | released
    # | budget_hold | resumed
    event: Mapped[str] = mapped_column(Text, nullable=False)
    iteration: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
