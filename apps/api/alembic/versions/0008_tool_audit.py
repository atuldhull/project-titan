"""The immutable tool_audit ledger (Phase 18; Vol 7 §4.5, TITAN-INT-023/024).

INSERT-only: ``tool_audit_block_mutation()`` rejects every UPDATE/DELETE at the
database, and rows are hash-chained per project (prev_hash → row_hash) so
tampering that bypasses the trigger is still evident to the verification job.
No ``set_updated_at`` trigger — the table can never be updated.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_audit",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True),
        sa.Column("tool_call_id", sa.Text, nullable=False),
        sa.Column("trace_id", sa.Text, nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("args_hash", sa.Text, nullable=False),
        sa.Column("args_redacted", JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        sa.Column("result_hash", sa.Text, nullable=True),
        sa.Column("initiator_chain", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("prev_hash", sa.Text, nullable=False),
        sa.Column("row_hash", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    )
    op.create_index("idx_tool_audit_project_seq", "tool_audit", ["project_id", "seq"])
    op.create_index("idx_tool_audit_call", "tool_audit", ["tool_call_id"])
    op.create_index("idx_tool_audit_task", "tool_audit", ["task_id"])

    op.execute(
        "CREATE OR REPLACE FUNCTION tool_audit_block_mutation() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'tool_audit is append-only (TITAN-INT-023)'; END; "
        "$$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_tool_audit_immutable BEFORE UPDATE OR DELETE ON tool_audit "
        "FOR EACH ROW EXECUTE FUNCTION tool_audit_block_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tool_audit_immutable ON tool_audit")
    op.execute("DROP FUNCTION IF EXISTS tool_audit_block_mutation()")
    op.drop_index("idx_tool_audit_task", table_name="tool_audit")
    op.drop_index("idx_tool_audit_call", table_name="tool_audit")
    op.drop_index("idx_tool_audit_project_seq", table_name="tool_audit")
    op.drop_table("tool_audit")
