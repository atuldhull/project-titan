"""event_inbox — consumer idempotency ledger (TITAN-BCK-010, Phase 10).

The outbox table already exists (migration 0003). This adds the inbox-pattern
ledger the demo domain consumer uses so replaying a stream is a no-op.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_inbox",
        # event_id ALONE is the PK: global idempotency so replaying the stream
        # into any consumer group is a no-op (TITAN-BCK-010 acceptance).
        sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("consumer_group", sa.Text, nullable=False),
        sa.Column("topic", sa.Text, nullable=False),
        sa.Column(
            "processed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
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
    op.execute(
        "CREATE TRIGGER trg_event_inbox_updated_at BEFORE UPDATE ON event_inbox "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.drop_table("event_inbox")
