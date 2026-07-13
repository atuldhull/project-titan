"""Technical-debt ledger (Phase 49; Vol 6 §5, TITAN-WFL-021).

Every accepted constitution deviation at review — an APPROVED_WITH_COMMENTS release
verdict or an accepted medium-severity security finding — records a debt ticket
here with a proposed paydown; the open total feeds the project dashboard. A ticket
is mutable (open → paid, paydown task linked later), so it carries the TITAN-BCK-008
convention columns and the shared set_updated_at trigger. Keyed uniquely by
(project_id, fingerprint) for idempotent re-runs.

Hand-written (autogenerate is forbidden).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

revision: str = "0026"
down_revision: str | None = "0025"
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
        "debt_tickets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False, server_default=sa.text("'medium'")),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("detail", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("paydown_proposal", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column(
            "paydown_task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        *_conventions(),
        sa.UniqueConstraint(
            "project_id", "fingerprint", name="uq_debt_tickets_project_fingerprint"
        ),
        sa.CheckConstraint("status IN ('open', 'paid')", name="ck_debt_tickets_status"),
        sa.CheckConstraint(
            "source IN ('code_review', 'security', 'constitution')",
            name="ck_debt_tickets_source",
        ),
    )
    op.create_index("ix_debt_tickets_project_status", "debt_tickets", ["project_id", "status"])
    op.execute(
        "CREATE TRIGGER trg_debt_tickets_updated_at BEFORE UPDATE ON debt_tickets "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_debt_tickets_updated_at ON debt_tickets")
    op.drop_index("ix_debt_tickets_project_status", table_name="debt_tickets")
    op.drop_table("debt_tickets")
