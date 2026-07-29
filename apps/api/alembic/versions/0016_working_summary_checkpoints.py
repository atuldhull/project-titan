# migrations: allow-exclusive — pre-Phase-76 index on a small pre-existing table.
"""Working-summary checkpoints (Phase 38; Vol 5 §8.2, TITAN-BRN-016).

``brain.working_summaries`` is the PostgreSQL checkpoint of each task's rolling
working-memory summary (`wm:{task_id}:summary` in Redis). Working memory lives
ONLY in Redis (TITAN-BRN-002) and Redis is disposable — the checkpoint bounds
what a Redis loss can cost: the runtime re-checkpoints on every fold, so a
flush loses at most the verbatim turns not yet folded (themselves re-derivable
from the thread's messages rows), never the accumulated summary.

The summary is stored as the fixed five-field JSONB schema (§8.2: Goal / Done
so far / Open threads / Constraints discovered / Next step) so the
TITAN-BRN-016 schema check applies to what is persisted, not just what is
generated.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "working_summaries",
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("summary", JSONB, nullable=False),
        sa.Column("fold_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        # The fold WATERMARK: how many of the thread's rendered turn lines have
        # already been folded into the summary — without it every turn would
        # re-fold the whole evicted prefix (duplicated content, wasted utility
        # calls). Monotonic, like fold_count.
        sa.Column("folded_through", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        schema="brain",
    )
    # The write-back flush is at-least-once (queue in Redis → transactional
    # write → ack): a crash between write and ack re-flushes, so each derived
    # event carries a deterministic wm_client_key and the flush skips keys
    # already durable. This expression index makes that dedup lookup O(log n).
    op.create_index(
        "ix_memory_records_wm_client_key",
        "memory_records",
        [sa.text("(metadata ->> 'wm_client_key')")],
        schema="brain",
        postgresql_where=sa.text("metadata ? 'wm_client_key'"),
    )


def downgrade() -> None:
    op.drop_index("ix_memory_records_wm_client_key", "memory_records", schema="brain")
    op.drop_table("working_summaries", schema="brain")
