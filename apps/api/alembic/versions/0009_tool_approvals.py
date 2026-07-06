"""Pending-approval records for admin-risk tool calls (Phase 19; Vol 7 §4.4,
TITAN-INT-022).

An unapproved admin-risk call creates a ``pending`` row; the human resolution
path (approve/deny endpoints) moves it to ``approved``/``denied``; the next
matching call consumes an ``approved`` row (single-use) and dispatches. A
partial unique index dedupes concurrent pendings per (project, tool, args).

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_approvals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("args_hash", sa.Text, nullable=False),
        sa.Column("args_redacted", JSONB, nullable=False),
        # pending | approved | denied | consumed
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("resolved_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
    op.create_index("idx_tool_approvals_project_status", "tool_approvals", ["project_id", "status"])
    # One live pending per (project, tool, args) — concurrent unapproved calls
    # dedupe onto it instead of flooding the dashboard.
    op.execute(
        "CREATE UNIQUE INDEX uq_tool_approvals_pending ON tool_approvals "
        "(project_id, tool, args_hash) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE TRIGGER trg_tool_approvals_updated_at BEFORE UPDATE ON tool_approvals "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tool_approvals_updated_at ON tool_approvals")
    op.execute("DROP INDEX IF EXISTS uq_tool_approvals_pending")
    op.drop_index("idx_tool_approvals_project_status", table_name="tool_approvals")
    op.drop_table("tool_approvals")
