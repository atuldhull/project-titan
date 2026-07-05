"""The immutable ``tool_audit`` ledger (TITAN-INT-023/024; owned per §3).

INSERT-only: a database trigger (migration 0008) rejects UPDATE and DELETE, and
rows are hash-chained per project (``prev_hash`` → ``row_hash``) so tampering
that bypasses the trigger is still evident to the verification job. Each tool
call appends a ``started`` row before dispatch and a terminal ``ok``/``error``
row after; pre-execution rejections append a single ``error`` row (every
rejection is audited, TITAN-SEC-031).
"""

import uuid
from typing import Any

from sqlalchemy import BigInteger, Identity, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class ToolAudit(ConventionsMixin, Base):
    __tablename__ = "tool_audit"
    __table_args__ = (
        Index("idx_tool_audit_project_seq", "project_id", "seq"),
        Index("idx_tool_audit_call", "tool_call_id"),
        Index("idx_tool_audit_task", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    # Total append order — the hash chain is verified per project in seq order.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    tool_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256 of the canonical raw arguments (attestation), alongside the
    # redacted copy that is safe to display (TITAN-INT-023).
    args_hash: Mapped[str] = mapped_column(Text, nullable=False)
    args_redacted: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # started | ok | error
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    initiator_chain: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    row_hash: Mapped[str] = mapped_column(Text, nullable=False)
