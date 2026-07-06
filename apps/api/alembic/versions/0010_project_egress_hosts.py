"""Admin-approved per-project egress hosts (Phase 23; Vol 8 §5 TITAN-SEC-043/033).

The sandbox's deny-all egress reaches only the registry mirror + the task git
remote by default; an Admin may opt a project into additional hosts here (the
SEC-033 approval surface). The per-task proxy credential embeds the project's
approved hosts at sandbox-create time, so the forward proxy allows them offline.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_egress_hosts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("host", sa.Text, nullable=False),
        sa.Column("approved_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        # ConventionsMixin (created_at/updated_at/version) — required on every table.
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
    # One row per (project, host); an idempotent opt-in never duplicates a host.
    op.create_index(
        "uq_project_egress_hosts", "project_egress_hosts", ["project_id", "host"], unique=True
    )
    op.execute(
        "CREATE TRIGGER trg_project_egress_hosts_updated_at BEFORE UPDATE ON project_egress_hosts "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_project_egress_hosts_updated_at ON project_egress_hosts")
    op.drop_index("uq_project_egress_hosts", table_name="project_egress_hosts")
    op.drop_table("project_egress_hosts")
