"""Immutable, hash-chained audit_events ledger (Phase 73; Vol 8 §10, SEC-090..094).

Append-only: ``audit_events_block_mutation()`` rejects every UPDATE and every
DELETE — EXCEPT a sanctioned retention DELETE that first sets the
``titan.audit_retention`` session GUC (the retention job sets it only AFTER
sealing the row to the age-encrypted archive, SEC-093). A single GLOBAL hash
chain — ``event_hash = sha256(prev_hash || canonical_event_json)`` — makes any
tamper that bypasses the trigger evident to the daily verification job (SEC-091).
No ``set_updated_at`` trigger: the table can never be updated.

Privilege note: TITAN runs one DB role (`titan`) which OWNS this table, and a
table owner is not subject to GRANT/REVOKE on its own DML — so the RAISE trigger
is what denies the app role's UPDATE/DELETE at the DB layer (the SEC-091
acceptance). True privilege-level INSERT-only would need a separate non-owner
role (a deploy concern). This is the same mechanism tool_audit (0008) and
decisions (0011) already use.

Rows carry references/digests only, never secret values/prompts/code bodies
(SEC-092) — the audit client redacts + digests before insert.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0037
Revises: 0036
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        # id IS the SEC-090 event_id.
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # Global monotonic sequence — the chain order + verification cursor.
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True),
        # The SEC-090 envelope. No FKs: an audit row must survive deletion of the
        # entity it names, and a failed-auth `actor` may be a non-existent account.
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("org", UUID(as_uuid=True), nullable=True),  # single-org: usually NULL
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("resource", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),  # success | failure | denied | ...
        sa.Column("source_ip", sa.Text, nullable=True),
        sa.Column("trace_id", sa.Text, nullable=True),  # links to the OTel trace
        # References/digests only (SEC-092) — never a secret value/prompt/code body.
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("prev_hash", sa.Text, nullable=False),
        sa.Column("event_hash", sa.Text, nullable=False),
        # Conventions columns (the schema census requires them); the block trigger
        # keeps updated_at == created_at and version == 1 forever.
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
    # SEC-094 search dimensions (actor, action, resource, time) + a retention scan.
    op.create_index("idx_audit_events_action", "audit_events", ["action", "created_at"])
    op.create_index("idx_audit_events_actor", "audit_events", ["actor", "created_at"])
    op.create_index("idx_audit_events_resource", "audit_events", ["resource", "created_at"])
    op.create_index("idx_audit_events_project", "audit_events", ["project_id", "created_at"])
    op.create_index("idx_audit_events_created", "audit_events", ["created_at"])

    op.execute(
        "CREATE OR REPLACE FUNCTION audit_events_block_mutation() RETURNS trigger AS $$ "
        "BEGIN "
        "  IF TG_OP = 'DELETE' AND current_setting('titan.audit_retention', true) = 'on' THEN "
        "    RETURN OLD; "  # sanctioned retention delete (row already sealed)
        "  END IF; "
        "  RAISE EXCEPTION 'audit_events is append-only (TITAN-SEC-091)'; "
        "END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION audit_events_block_mutation()"
    )

    # Seal ledger (SEC-093): which UTC days have been sealed to the age-encrypted
    # archive. audit_events is immutable (no per-row sealed_at can be UPDATEd), so
    # sealing is tracked here; the retention job deletes online rows only for days
    # recorded here. A mutable metadata table (not hash-chained — audit_events is
    # the integrity anchor).
    op.create_table(
        "audit_seals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("day", sa.Date, nullable=False, unique=True),
        sa.Column(
            "segment_ref", sa.Text, nullable=False
        ),  # object-storage path of the sealed segment
        sa.Column("event_count", sa.Integer, nullable=False),
        sa.Column("max_seq", sa.BigInteger, nullable=False),  # highest audit seq archived here
        sa.Column("digest", sa.Text, nullable=False),  # sha256 of the sealed plaintext segment
        # event_hash of the max_seq row — the archived chain TIP. verify_chain
        # anchors the first ONLINE row's prev_hash to this, so a prefix delete (or
        # whole-ledger wipe) NOT backed by a seal is detected (SEC-091).
        sa.Column("tip_hash", sa.Text, nullable=False),
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
        "CREATE TRIGGER trg_audit_seals_updated_at BEFORE UPDATE ON audit_seals "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_seals_updated_at ON audit_seals")
    op.drop_table("audit_seals")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_immutable ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_block_mutation()")
    for idx in (
        "idx_audit_events_created",
        "idx_audit_events_project",
        "idx_audit_events_resource",
        "idx_audit_events_actor",
        "idx_audit_events_action",
    ):
        op.drop_index(idx, table_name="audit_events")
    op.drop_table("audit_events")
