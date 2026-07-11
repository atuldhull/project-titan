"""Decay/curation state + the cold-body store + retrieval feedback (Vol 5 §9,
§11, §12).

Adds what the nightly curation job and the learning loops need:
- ``brain.memory_records.demoted`` — the §9.2 Demote flag: the record stays
  ``status='active'`` but is excluded from default retrieval (a payload flag in
  Qdrant too) until deep search or an access restores it (TITAN-BRN-020).
- ``brain.memory_records.low_decay_since`` — the Archive clock: when decay first
  drops below the archive threshold this is stamped; Archive fires only after it
  has stayed low for 30 consecutive days (§9.2). Cleared when decay recovers.
- ``brain.memory_bodies_cold`` — the §9.2 Compact cold store: an archived
  episodic record's full body is moved here and its live ``body_md`` replaced by
  its summary line (partially reversible).
- ``brain.retrieval_feedback`` — TITAN-BRN-026: weekly packet/citation counts per
  record class, the signal the curation review folds into importance weighting.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import BIGINT, TIMESTAMP, UUID

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- §9.2 Demote flag + Archive clock on the canonical record --------------
    op.add_column(
        "memory_records",
        sa.Column("demoted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        schema="brain",
    )
    op.add_column(
        "memory_records",
        sa.Column("low_decay_since", TIMESTAMP(timezone=True), nullable=True),
        schema="brain",
    )
    # The curation scan walks active records; a demoted record is the common
    # default-retrieval exclusion, so index the (status, demoted) shape.
    op.create_index(
        "ix_memory_records_status_demoted",
        "memory_records",
        ["status", "demoted"],
        schema="brain",
    )

    # -- §9.2 Compact: the cold body store ------------------------------------
    op.create_table(
        "memory_bodies_cold",
        sa.Column(
            "record_id",
            UUID(as_uuid=True),
            sa.ForeignKey("brain.memory_records.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("body_md", sa.Text, nullable=False),
        sa.Column(
            "moved_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        schema="brain",
    )

    # -- TITAN-BRN-026: weekly retrieval feedback per record class -------------
    op.create_table(
        "retrieval_feedback",
        sa.Column("id", BIGINT, sa.Identity(always=False), primary_key=True),
        sa.Column("week", sa.Date, nullable=False),
        sa.Column("record_class", sa.Text, nullable=False),
        sa.Column("packets", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("citations", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("week", "record_class", name="uq_retrieval_feedback_week_class"),
        schema="brain",
    )


def downgrade() -> None:
    op.drop_table("retrieval_feedback", schema="brain")
    op.drop_table("memory_bodies_cold", schema="brain")
    op.drop_index("ix_memory_records_status_demoted", table_name="memory_records", schema="brain")
    op.drop_column("memory_records", "low_decay_since", schema="brain")
    op.drop_column("memory_records", "demoted", schema="brain")
