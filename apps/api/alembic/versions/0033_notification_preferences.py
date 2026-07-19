"""Per-user notification channel routing (Phase 64; Vol 4 §7.1, §6.13).

§7.1 specifies a category × channel matrix and states that "per-category routing
is user-configurable (channel matrix in Settings)". Half of that already
existed: ``notifications.category`` has carried approval|failure|progress|
activity|security since 0003. The CHANNEL axis had no storage anywhere in the
schema, so Settings → Notifications had nothing to write to.

Sparse by design — a row exists only where a user has overridden a §7.1 default,
and the absence of a row means "track the default". Seeding a full matrix per
user would pin every default at signup time, so a later change to a default
would silently reach new users only.

``channels`` is JSONB rather than a child table: the value set is four fixed
strings that change with the spec rather than with data, and the matrix is
always read and written whole.

Hand-written (autogenerate is forbidden); ConventionsMixin's created_at/
updated_at/version are spelled out, as every migration here does.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: None = None
depends_on: None = None

_TABLE = "notification_preferences"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column(
            "channels",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # One row per user per category. The read merges these over the §7.1
        # defaults, so a duplicate would make the effective matrix depend on
        # row order — a bug that would surface as a preference that "sometimes"
        # reverts.
        sa.UniqueConstraint(
            "user_id", "category", name="uq_notification_preferences_user_category"
        ),
    )
    op.execute(
        f"CREATE TRIGGER trg_{_TABLE}_updated_at BEFORE UPDATE ON {_TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_updated_at ON {_TABLE}")
    op.drop_table(_TABLE)
