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
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID
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


class AgentScorecard(ConventionsMixin, Base):
    """Per-terminal-task evaluation (Vol 10 §3, TITAN-EVO-005). Effectively
    append-only (no set_updated_at trigger, like audit_events) but carries the
    created_at/updated_at/version convention columns; keyed to the exact
    prompt/spec/model versions that produced the task."""

    __tablename__ = "agent_scorecards"
    __table_args__ = (
        Index("ix_agent_scorecards_agent_class_created", "agent_id", "task_class", "created_at"),
        Index("uq_agent_scorecards_task", "task_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)  # roster id, e.g. 'eng_backend'
    agent_spec_sha: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_sha: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_class: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    composite_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False)
    wall_clock_s: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)


class RootCauseRecord(ConventionsMixin, Base):
    """A five-whys root-cause analysis of a classified failure (Vol 10 §5.2,
    TITAN-EVO-012). failure_code is CHECK-bound to the fixed F-* taxonomy."""

    __tablename__ = "root_cause_records"
    __table_args__ = (
        CheckConstraint(
            "failure_code IN ('F-REQ','F-CTX','F-GEN','F-STD','F-SEC','F-ORCH','F-TOOL','F-LLM',"
            "'F-HUM','F-UNK')",
            name="ck_root_cause_records_code",
        ),
        CheckConstraint(
            "severity IN ('low','medium','high','critical')", name="ck_root_cause_records_severity"
        ),
        CheckConstraint(
            "status IN ('open','remediated','accepted_risk')", name="ck_root_cause_records_status"
        ),
        Index(
            "uq_root_cause_records_open_signature",
            "signature",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_root_cause_records_status", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    failure_code: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    task_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    five_whys: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    contributing: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    remediation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    ep: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    ep_merged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
