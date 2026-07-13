"""Local deployment records (Phase 48; Vol 7 §6.7 TITAN-INT-046, Vol 1
TITAN-FND-027).

The ``titan.local`` connector's persistent deployment record: deploying a
generated app locally allocates a port, brings its Compose stack up, health-
checks it, and records the reachable URL + status here. A deployment is *mutable*
(status/url change as the stack comes up, fails, or is torn down), so it carries
the TITAN-BCK-008 convention columns and the shared ``set_updated_at`` trigger.
The ``UNIQUE(project_id, name)`` key gives re-deploy idempotency: re-deploying the
same app updates its row in place.

Hand-written (autogenerate is forbidden).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: None = None
depends_on: None = None


def _conventions() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    ]


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("compose_file", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("port", sa.Integer, nullable=True),
        sa.Column("services", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("health", sa.Text, nullable=False, server_default=sa.text("'unknown'")),
        sa.Column("detail", sa.Text, nullable=False, server_default=sa.text("''")),
        *_conventions(),
        sa.UniqueConstraint("project_id", "name", name="uq_deployments_project_name"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'failed', 'stopped')",
            name="ck_deployments_status",
        ),
        sa.CheckConstraint("health IN ('unknown', 'green', 'red')", name="ck_deployments_health"),
    )
    op.execute(
        "CREATE TRIGGER trg_deployments_updated_at BEFORE UPDATE ON deployments "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_deployments_updated_at ON deployments")
    op.drop_table("deployments")
