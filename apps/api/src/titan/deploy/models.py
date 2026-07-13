"""Deployment persistence (Vol 7 §6.7, TITAN-INT-046). Private to the module —
only ``service`` crosses the boundary. A deployment is a *mutable* record (its
status and reachable URL change as the stack comes up, fails, or is torn down),
so it carries ``created_at``/``updated_at``/``version`` and the shared
``set_updated_at`` trigger (TITAN-BCK-008)."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, uuid7


class Deployment(Base):
    """One local Compose deployment of a generated app, keyed uniquely by
    ``(project_id, name)`` so re-deploying the same app updates its record in
    place rather than accreting duplicates (re-deploy idempotency)."""

    __tablename__ = "deployments"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_deployments_project_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # The task whose agents deployed it (nullable: a deployment outlives its task).
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    # The generated app / compose-stack name — the idempotency key with project_id.
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Workspace-relative path to the app's own docker-compose.yml.
    compose_file: Mapped[str] = mapped_column(Text, nullable=False)
    # pending | running | failed | stopped
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    # The host-reachable URL once running (null while pending / after teardown).
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The published app port the URL fronts (null until known).
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The compose service names the stack brought up.
    services: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # unknown | green | red — the last health-check verdict.
    health: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'unknown'"))
    # Human/agent-readable last-status detail (e.g. a health-check failure reason).
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
