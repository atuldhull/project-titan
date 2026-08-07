"""A/B deployments + arms (Phase 86, Vol 10 §6 EVO-018 + §2 EVO-003).

``evolution_deployments`` is an online A/B deployment of a challenger config under a
monitoring contract (KPI floor + observation window) that the auto-rollback monitor
enforces; at most one active (shadow|canary) deployment per agent scope. ``evolution_ab_arms``
records each arm's (champion|challenger) score for a task so a sequential test can
compare them — kept separate from the UNIQUE-per-task ``agent_scorecards``.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0041
Revises: 0040
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _conventions() -> list[sa.Column]:
    return [
        sa.Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    ]


def upgrade() -> None:
    op.create_table(
        "evolution_deployments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("ep", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'shadow'")),
        sa.Column("canary_pct", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("champion_ref", sa.Text, nullable=False),
        sa.Column("challenger_ref", sa.Text, nullable=False),
        sa.Column("kpi_floor", sa.Numeric(5, 2), nullable=False),
        sa.Column("min_samples", sa.Integer, nullable=False, server_default=sa.text("5")),
        sa.Column(
            "observation_window_s", sa.Integer, nullable=False, server_default=sa.text("300")
        ),
        sa.Column("metrics", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("rolled_back_at", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rollback_reason", sa.Text, nullable=False, server_default=sa.text("''")),
        *_conventions(),
        sa.CheckConstraint(
            "status IN ('shadow','canary','promoted','rolled_back','aborted')",
            name="ck_evolution_deployments_status",
        ),
        sa.CheckConstraint("mode IN ('shadow','canary')", name="ck_evolution_deployments_mode"),
        sa.CheckConstraint(
            "canary_pct >= 0 AND canary_pct <= 100", name="ck_evolution_deployments_pct"
        ),
    )
    # At most one active (shadow|canary) deployment per agent scope.
    op.create_index(
        "uq_evolution_deployments_active_agent",
        "evolution_deployments",
        ["agent_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('shadow','canary')"),
    )
    op.create_index(
        "ix_evolution_deployments_status", "evolution_deployments", ["status", "created_at"]
    )
    op.execute(
        "CREATE TRIGGER trg_evolution_deployments_updated_at BEFORE UPDATE ON "
        "evolution_deployments FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.create_table(
        "evolution_ab_arms",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "deployment_id",
            UUID(as_uuid=True),
            sa.ForeignKey("evolution_deployments.id"),
            nullable=False,
        ),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("arm", sa.Text, nullable=False),
        sa.Column("mode", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("task_class", sa.Text, nullable=False),
        sa.Column("composite_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("dimensions", JSONB, nullable=False),
        sa.Column("prompt_sha", sa.Text, nullable=False),
        sa.Column("model_id", sa.Text, nullable=False),
        *_conventions(),
        sa.CheckConstraint("arm IN ('champion','challenger')", name="ck_evolution_ab_arms_arm"),
        sa.CheckConstraint("mode IN ('shadow','canary')", name="ck_evolution_ab_arms_mode"),
    )
    op.create_index(
        "uq_evolution_ab_arms_dep_task_arm",
        "evolution_ab_arms",
        ["deployment_id", "task_id", "arm"],
        unique=True,
    )
    op.create_index(
        "ix_evolution_ab_arms_deployment_arm",
        "evolution_ab_arms",
        ["deployment_id", "arm", "created_at"],
    )
    op.execute(
        "CREATE TRIGGER trg_evolution_ab_arms_updated_at BEFORE UPDATE ON "
        "evolution_ab_arms FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_evolution_ab_arms_updated_at ON evolution_ab_arms")
    op.drop_index("ix_evolution_ab_arms_deployment_arm", table_name="evolution_ab_arms")
    op.drop_index("uq_evolution_ab_arms_dep_task_arm", table_name="evolution_ab_arms")
    op.drop_table("evolution_ab_arms")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evolution_deployments_updated_at ON evolution_deployments"
    )
    op.drop_index("ix_evolution_deployments_status", table_name="evolution_deployments")
    op.drop_index("uq_evolution_deployments_active_agent", table_name="evolution_deployments")
    op.drop_table("evolution_deployments")
