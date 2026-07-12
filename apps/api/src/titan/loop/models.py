"""Self-improvement-loop persistence (Vol 6 §3). Private to the module — only
``service`` crosses the boundary. Mutable records carry ``set_updated_at``; the
cycle + stage-event ledgers are append-only (DB trigger)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, uuid7


class LoopRecord(Base):
    """One self-improvement loop per task (TITAN-WFL-010). A task cannot reach
    MERGED without one whose ``status`` is ``converged``."""

    __tablename__ = "loop_records"
    __table_args__ = (
        UniqueConstraint("task_id", "iteration", name="uq_loop_records_task_iteration"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # running | converged | escalated | arbitration | superseded
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    cycle_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_cycles: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))
    # converged | exhausted (null while running)
    termination_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class LoopCycle(Base):
    """One append-only row per completed cycle: convergence bookkeeping so a
    converged loop is distinguishable from an exhausted one (TITAN-WFL-013)."""

    __tablename__ = "loop_cycles"
    __table_args__ = (
        UniqueConstraint("loop_id", "cycle_number", name="uq_loop_cycles_loop_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    loop_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("loop_records.id", ondelete="CASCADE"), nullable=False
    )
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    new_findings_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    clean: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    # continue | converged | exhausted | arbitration
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class LoopFinding(Base):
    """A review finding with its refute-or-fix disposition (TITAN-WFL-012)."""

    __tablename__ = "loop_findings"
    __table_args__ = (
        UniqueConstraint("loop_id", "fingerprint", name="uq_loop_findings_loop_fingerprint"),
        Index("ix_loop_findings_loop", "loop_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    loop_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("loop_records.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    raised_cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    file: Mapped[str] = mapped_column(Text, nullable=False)
    line: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    hunk_key: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # open | fixed | refuted | upheld | arbitration
    disposition: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    refutation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    refute_upheld_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    conflict_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class LoopStageEvent(Base):
    """Append-only audit ledger of every stage execution. The REVIEW row records
    the reviewer's isolated-context manifest — the TITAN-WFL-011 audit."""

    __tablename__ = "loop_stage_events"
    __table_args__ = (Index("ix_loop_stage_events_loop", "loop_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    loop_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("loop_records.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    cycle_number: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'n/a'"))
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
