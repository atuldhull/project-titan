"""Two-phase drain marker for the embedding-pipeline outbox (Phase 35).

``brain.ingest_outbox`` gains ``published_at``, mirroring the Phase-10 core
outbox (TITAN-BCK-005): the relay stamps it when the row is published to the
``titan:memory:ingest`` stream, and the embedder stamps ``processed_at`` when
the vector is confirmed in Qdrant. A row published long ago but never
processed is REPUBLISHED under its stable event id (PostgreSQL is canonical —
ADR-05-01 — so a wiped Redis heals from here), and consumers dedup/idempotent-
upsert, making the whole pipeline at-least-once with exactly-once effect.

``dead_at`` makes dead-lettering TERMINAL in the canonical store: the reaper
stamps it when the bus routes the row's message to the DLQ after MAX_ATTEMPTS
deliveries (§5.1), and the relay's republish predicate excludes dead rows —
without it, the stale-republish loop would resurrect poison forever, growing
the DLQ and burning gateway spend unboundedly. A future DLQ-replay tool clears
``dead_at`` (and ``published_at``) to explicitly re-enter the pipeline.

The relay's drain query is served by the existing partial index
``ix_ingest_outbox_unprocessed`` (WHERE processed_at IS NULL, from 0014) —
both the fresh drain and the stale-republish scan filter on unprocessed rows.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingest_outbox",
        sa.Column("published_at", TIMESTAMP(timezone=True), nullable=True),
        schema="brain",
    )
    op.add_column(
        "ingest_outbox",
        sa.Column("dead_at", TIMESTAMP(timezone=True), nullable=True),
        schema="brain",
    )


def downgrade() -> None:
    op.drop_column("ingest_outbox", "dead_at", schema="brain")
    op.drop_column("ingest_outbox", "published_at", schema="brain")
