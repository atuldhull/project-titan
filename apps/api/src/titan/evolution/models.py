"""The ``evolution_artifacts`` ledger (Vol 10 §2, TITAN-EVO-002).

Stores ONLY immutable references — Git commit SHA + repo path + content hash +
the approval signature (approved_by / approved_at / ep / diff_url). No mutable
prompt text ever lives in the database (TITAN-EVO-002c); the bytes live in
``/evolution/**`` under git. This table is the queryable, auditable record of
which artifact versions were approved, synced from the manifest and appended to by
the human-gated approve action; it is never a second prompt source.
"""

import uuid
from datetime import datetime

from sqlalchemy import Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class EvolutionArtifact(ConventionsMixin, Base):
    __tablename__ = "evolution_artifacts"
    __table_args__ = (
        # One row per (path, content hash): re-approving or re-syncing the same
        # bytes is idempotent; a new content hash is a new immutable row.
        UniqueConstraint("path", "content_sha256", name="uq_evolution_artifacts_path_sha"),
        Index("ix_evolution_artifacts_kind_ref", "kind", "ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # prompt|prompt_base|routing|...
    ref: Mapped[str] = mapped_column(Text, nullable=False)  # agent_id / 'base' / 'routing'
    path: Mapped[str] = mapped_column(Text, nullable=False)  # repo-relative POSIX path
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)  # the prompt SHA
    commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)  # approving HEAD
    # server_defaults mirror migration 0038 so ORM metadata and the DDL agree.
    semver: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'0.0.0'"))
    ep: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # The EP gate (TITAN-EVO-001): a human user id + a signed timestamp. Nullable
    # so an in-flight (proposed-but-unapproved) artifact can be recorded too.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    diff_url: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
