"""Execution tables: runs, artifacts (Vol 3 §6; owned per §3)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class Run(ConventionsMixin, Base):
    __tablename__ = "runs"
    __table_args__ = (
        # One attempt number per task (§8.2 attempt = max+1); see migration 0006.
        UniqueConstraint("task_id", "attempt", name="uq_runs_task_id_attempt"),
        Index("idx_runs_task", "task_id", text("attempt DESC")),
        Index(
            "idx_runs_status",
            "status",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # queued|running|succeeded|failed|cancelled|timed_out
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    # resolved prompt context refs
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    tokens_out: Mapped[int | None] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), server_default=text("0"))
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class Artifact(ConventionsMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("idx_artifacts_project_kind", "project_id", "kind"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # code_patch|file|test_report|doc|diagram|build_log
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # repo-relative path if applicable
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # object-store/filesystem key
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
