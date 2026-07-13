"""Planning persistence (Vol 6 §4, TITAN-WFL-016/018/019). Private to the module —
only ``service`` crosses the boundary. Three mutable tables carry the TITAN-BCK-008
convention columns and the shared ``set_updated_at`` trigger.

* ``sprint_ceremonies`` — the WFL-016 planning/review/retrospective records.
* ``milestone_states`` — the single current-milestone row per project (WFL-018).
* ``task_blocks`` — the WFL-019 blocker ledger + the recovery-ladder rung reached.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, uuid7


class SprintCeremony(Base):
    """One automated sprint ceremony record (TITAN-WFL-016). Keyed uniquely by
    ``(project_id, sprint_number, kind)`` so re-running a ceremony overwrites rather
    than duplicates — 'every completed sprint has all three records' stays exact."""

    __tablename__ = "sprint_ceremonies"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "sprint_number", "kind", name="uq_sprint_ceremonies_project_sprint_kind"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    sprint_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # planning | review | retrospective
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # The ceremony payload: goals/committed_stories/risks (planning); demo_links/
    # velocity_actual/velocity_estimate (review); changes (retrospective).
    body: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # The organization-memory record the retrospective/lesson was filed as (nullable).
    memory_record_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class MilestoneState(Base):
    """The project's current milestone (TITAN-WFL-018). One row per project; it
    always equals the highest contiguous milestone whose DoD holds."""

    __tablename__ = "milestone_states"
    __table_args__ = (UniqueConstraint("project_id", name="uq_milestone_states_project"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # none | M0 | M1 | M2 | M3 | M4
    current_milestone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'none'")
    )
    # The last evaluated signals snapshot (for the dashboard + audit).
    signals: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class TaskBlock(Base):
    """A detected task block + the recovery-ladder rung reached (TITAN-WFL-019). At
    most one OPEN block per task (enforced by a partial unique index in the
    migration); resolved blocks are retained for the risk register."""

    __tablename__ = "task_blocks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    # dependency_failed | awaiting_clarification | loop_escalation | stalled
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # The recovery-ladder rung last performed (dependency_recheck … human_escalation).
    rung: Mapped[str] = mapped_column(Text, nullable=False)
    # open | recovered | escalated
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    detected_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
