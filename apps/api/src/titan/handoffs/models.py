"""Handoff persistence tables: handoff_artifacts + the append-only gate ledger
(Vol 6 §2.2–2.3; owned per §3). Private to the module — only ``service`` crosses
the boundary."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, uuid7


class HandoffArtifact(Base):
    """The versioned, schema-validated §2.3 artifact a producing stage emits
    (TITAN-WFL-003). ``version`` is monotonic per (project, stage) — a resubmission
    is a new version, never an overwrite (the artifact history is preserved)."""

    __tablename__ = "handoff_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "stage", "version", name="uq_handoff_artifacts_project_stage_version"
        ),
        Index("ix_handoff_artifacts_project_id", "project_id", "stage"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    submitted_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class GateEvaluation(Base):
    """One append-only row per gate evaluation (TITAN-WFL-004): verdict + evaluator
    identity + reasons + attempt + timestamp. An UPDATE/DELETE is rejected by a DB
    trigger (TITAN-AGT-019). ``artifact_id`` is null when schema validation failed
    before a valid artifact could be stored."""

    __tablename__ = "gate_evaluations"
    __table_args__ = (Index("ix_gate_evaluations_project_id", "project_id", "stage", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("handoff_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    # pass | fail
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    reasons: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    evaluator_agent: Mapped[str] = mapped_column(Text, nullable=False)
    evaluator_model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
