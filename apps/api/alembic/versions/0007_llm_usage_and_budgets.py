"""LLM accounting + budget tables (Phase 17; Vol 7 §8.4, TITAN-OPS-021).

``llm_usage`` — append-only per-call ledger (tokens, cost, provider/model served,
degraded flag) that is both the cost-accounting table and the fallback audit
trail. ``budget_holds`` — persisted project/task pause flag checked pre-request.

Hand-written per §6 (autogenerate forbidden); both carry the conventions columns
and the shared updated_at trigger.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ["llm_usage", "budget_holds"]


def _conventions() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", sa.Text, nullable=True),
        sa.Column("requested_alias", sa.Text, nullable=True),
        sa.Column("served_alias", sa.Text, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("cached_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("degraded_model", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("price_version", sa.Integer, nullable=False, server_default=sa.text("1")),
        *_conventions(),
    )
    op.create_index(
        "idx_llm_usage_project", "llm_usage", ["project_id", sa.text("created_at DESC")]
    )
    op.create_index("idx_llm_usage_task", "llm_usage", ["task_id"])

    op.create_table(
        "budget_holds",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("scope_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("threshold_pct", sa.Integer, nullable=False, server_default=sa.text("100")),
        *_conventions(),
        sa.UniqueConstraint("scope", "scope_id", name="uq_budget_holds_scope"),
    )

    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
    op.drop_table("budget_holds")
    op.drop_index("idx_llm_usage_task", table_name="llm_usage")
    op.drop_index("idx_llm_usage_project", table_name="llm_usage")
    op.drop_table("llm_usage")
