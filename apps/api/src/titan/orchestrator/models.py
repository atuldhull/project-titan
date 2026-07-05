"""Orchestration tables: tasks, task_dependencies (Vol 3 §6; owned per §3)."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
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
