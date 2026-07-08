"""The memory core: schema ``brain``, Memory records/chunks/outbox + audit ledger.

Creates the Vol 5 §4.3 memory core VERBATIM in a dedicated ``brain`` schema:
- ``brain.memory_records`` — the canonical, auditable store of every long-term
  memory (PostgreSQL is source of truth, Qdrant a derived index; ADR-05-01). A
  STORED generated ``tsv`` column + GIN index is the lexical leg of the §6 hybrid
  search; ``(scope, kind, status)`` indexes the payload-filtered retrieval path.
- ``brain.memory_chunks`` — the embeddable units; ``id`` doubles as the Qdrant
  point id (ADR-05-01), so reconciliation is a set difference.
- ``brain.ingest_outbox`` — the transactional outbox the §5.1 embedding pipeline
  relays to the ``titan:memory:ingest`` stream (at-least-once, idempotent upsert).
- ``brain.memory_ledger`` — the append-only access/audit trail: one row per
  memory read/write/promote/prune/supersede (TITAN-BRN-005).

These tables intentionally do NOT carry the ConventionsMixin (updated_at/version):
§4.3 fixes their columns exactly, and the ledger/outbox are append-only.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, BIGINT, JSONB, TIMESTAMP, TSVECTOR, UUID

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS brain")

    # -- brain.memory_records: the canonical memory store (§4.3) ---------------
    op.create_table(
        "memory_records",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "kind",
            sa.Text,
            nullable=False,
        ),
        sa.Column("scope", sa.Text, nullable=False),  # 'org' | 'project:<uuid>'
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("source_agent", sa.Text, nullable=False),
        sa.Column("source_task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body_md", sa.Text, nullable=False),  # canonical content, Markdown
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("importance", sa.REAL, nullable=False, server_default=sa.text("0.5")),
        sa.Column("decay_score", sa.REAL, nullable=False, server_default=sa.text("1.0")),
        sa.Column("access_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_accessed_at", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "superseded_by",
            UUID(as_uuid=True),
            sa.ForeignKey("brain.memory_records.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        # The lexical leg of §6 hybrid search — recomputed by Postgres on write.
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', title || ' ' || body_md)", persisted=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "kind IN ('episodic','semantic','procedural')", name="ck_memory_records_kind"
        ),
        sa.CheckConstraint(
            "status IN ('active','archived','superseded','deleted')",
            name="ck_memory_records_status",
        ),
        schema="brain",
    )
    op.create_index(
        "ix_memory_records_tsv",
        "memory_records",
        ["tsv"],
        schema="brain",
        postgresql_using="gin",
    )
    op.create_index(
        "ix_memory_records_scope_kind_status",
        "memory_records",
        ["scope", "kind", "status"],
        schema="brain",
    )

    # -- brain.memory_chunks: embeddable units; id == Qdrant point id ----------
    op.create_table(
        "memory_chunks",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "record_id",
            UUID(as_uuid=True),
            sa.ForeignKey("brain.memory_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=False),
        sa.Column("embed_model", sa.Text, nullable=True),  # e.g. 'nomic-embed-text-v1.5'
        sa.Column("embedded_at", TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("record_id", "seq", name="uq_memory_chunks_record_id_seq"),
        schema="brain",
    )

    # -- brain.ingest_outbox: the §5.1 embedding-pipeline outbox ---------------
    op.create_table(
        "ingest_outbox",
        sa.Column("id", BIGINT, sa.Identity(always=False), primary_key=True),
        sa.Column(
            "chunk_id",
            UUID(as_uuid=True),
            sa.ForeignKey("brain.memory_chunks.id"),
            nullable=False,
        ),
        sa.Column("op", sa.Text, nullable=False),
        sa.Column(
            "enqueued_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("processed_at", TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("op IN ('upsert','delete')", name="ck_ingest_outbox_op"),
        schema="brain",
    )
    op.create_index(
        "ix_ingest_outbox_unprocessed",
        "ingest_outbox",
        ["id"],
        schema="brain",
        postgresql_where=sa.text("processed_at IS NULL"),
    )

    # -- brain.memory_ledger: append-only access/audit trail (TITAN-BRN-005) ---
    op.create_table(
        "memory_ledger",
        sa.Column("id", BIGINT, sa.Identity(always=False), primary_key=True),
        sa.Column("at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("actor_agent", sa.Text, nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("record_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "action IN ('read','write','promote','prune','supersede')",
            name="ck_memory_ledger_action",
        ),
        schema="brain",
    )
    op.create_index(
        "ix_memory_ledger_records",
        "memory_ledger",
        ["record_ids"],
        schema="brain",
        postgresql_using="gin",
    )
    # Append-only at the DB layer (TITAN-BRN-005): the audit trail cannot be
    # rewritten. Reuses the shared reject_decision_mutation() from migration 0011.
    op.execute(
        "CREATE TRIGGER trg_memory_ledger_append_only "
        "BEFORE UPDATE OR DELETE ON brain.memory_ledger "
        "FOR EACH ROW EXECUTE FUNCTION reject_decision_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_memory_ledger_append_only ON brain.memory_ledger")
    op.drop_table("memory_ledger", schema="brain")
    op.drop_table("ingest_outbox", schema="brain")
    op.drop_table("memory_chunks", schema="brain")
    op.drop_table("memory_records", schema="brain")
    op.execute("DROP SCHEMA IF EXISTS brain CASCADE")
