"""Persistence for the derived project artifacts (Vol 6 §5, TITAN-WFL-020). Private
to the module — only ``service`` crosses the boundary. A regeneration that yields
new content appends a version row; the current artifact is the max artifact_version
per (project, kind)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, uuid7


class DerivedArtifact(Base):
    """One versioned rendering of a §5 artifact. Content is deterministic (no
    wall-clock), so ``content_hash`` is both the change detector (skip a no-op
    regeneration) and the replay oracle (delete-and-regenerate reproduces it)."""

    __tablename__ = "derived_artifacts"
    __table_args__ = (
        UniqueConstraint("project_id", "kind", "artifact_version", name="uq_derived_artifacts_pkv"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # roadmap | kanban | dependency_graph | release_plan | risk_analysis | debt_report
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    content_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
