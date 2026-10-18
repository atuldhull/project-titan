"""Agent scorecards — per-terminal-task evaluation (Phase 84, Vol 10 §3, TITAN-EVO-005).

Append-only evaluation record: one row per terminal task, scoring 8 dimensions and
pinned to the exact agent-spec SHA, prompt SHA, model, and OTel trace that produced
it. No updated_at/version — a scorecard is immutable once written. Index on
(agent_id, task_class, created_at) backs the KPI rollup's per-agent/per-class window
queries (TITAN-EVO-006).

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0039
Revises: 0038
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_scorecards",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),  # roster id, e.g. 'eng_backend'
        sa.Column("agent_spec_sha", sa.Text, nullable=False),
        sa.Column("prompt_sha", sa.Text, nullable=False),
        sa.Column("model_id", sa.Text, nullable=False),
        sa.Column("task_class", sa.Text, nullable=False),
        sa.Column("dimensions", JSONB, nullable=False),
        sa.Column("composite_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_in", sa.BigInteger, nullable=False),
        sa.Column("tokens_out", sa.BigInteger, nullable=False),
        sa.Column("wall_clock_s", sa.Integer, nullable=False),
        sa.Column("trace_id", sa.Text, nullable=False),
        # created_at/updated_at/version per the TITAN table convention (like the
        # append-only audit_events): scorecards are never updated, so there is no
        # set_updated_at trigger, but the columns exist for the convention.
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    )
    op.create_index(
        "ix_agent_scorecards_agent_class_created",
        "agent_scorecards",
        ["agent_id", "task_class", "created_at"],
    )
    # Exactly ONE scorecard per terminal task: makes generation idempotent even
    # under a consumer redelivery/reap race (two consumers can't double-count KPIs).
    op.create_index("uq_agent_scorecards_task", "agent_scorecards", ["task_id"], unique=True)

    # evolution_investigations (TITAN-EVO-007): a sustained agent-KPI drop opens an
    # investigation for the Memory Agent. It is deliberately NOT a project `tasks`
    # row — tasks are project-scoped (FK project_id) while an agent-KPI regression is
    # cross-project — so it is a dedicated, agent-scoped record carrying the KPI
    # series snapshot that triggered it.
    op.create_table(
        "evolution_investigations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("task_class", sa.Text, nullable=False),
        sa.Column("kpi", sa.Text, nullable=False, server_default=sa.text("'composite'")),
        sa.Column("window_days", sa.Integer, nullable=False),
        sa.Column("recent_value", sa.Numeric(6, 2), nullable=False),
        sa.Column("baseline_value", sa.Numeric(6, 2), nullable=False),
        sa.Column("drop_pct", sa.Numeric(6, 2), nullable=False),
        sa.Column("series_snapshot", JSONB, nullable=False),
        sa.Column("assignee_role", sa.Text, nullable=False, server_default=sa.text("'memory'")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    )
    # A resolved investigation flips status; keep updated_at fresh via the trigger.
    op.execute(
        "CREATE TRIGGER trg_evolution_investigations_updated_at BEFORE UPDATE ON "
        "evolution_investigations FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    # At most ONE open investigation per (agent, task_class) — re-detecting the same
    # degradation is idempotent; resolved rows accumulate freely.
    op.create_index(
        "uq_evolution_investigations_open",
        "evolution_investigations",
        ["agent_id", "task_class"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_evolution_investigations_status", "evolution_investigations", ["status", "created_at"]
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evolution_investigations_updated_at ON evolution_investigations"
    )
    op.drop_index("ix_evolution_investigations_status", table_name="evolution_investigations")
    op.drop_table("evolution_investigations")
    op.drop_index("uq_agent_scorecards_task", table_name="agent_scorecards")
    op.drop_index("ix_agent_scorecards_agent_class_created", table_name="agent_scorecards")
    op.drop_table("agent_scorecards")
