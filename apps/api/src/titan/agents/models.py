"""Agent registry table (Vol 3 §6; owned per §3)."""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class Agent(ConventionsMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (Index("idx_agents_project_role", "project_id", "role"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    # NULL = org-level template
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    # ceo|pm|project_manager|architect|backend|frontend|db|devops|qa|security|
    # reviewer|docs|research|memory (Vol 2 roster; text per §6 notes)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # idle|busy|paused|error
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'idle'"))
    # {"provider":"ollama","model":"qwen2.5-coder:14b","temperature":0.2}
    model_binding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # prompt template ref, tool allowlist (Vol 2)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Plain uuid on purpose (spec §6): an FK to tasks would create a circular
    # dependency with tasks' agent FKs.
    current_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
