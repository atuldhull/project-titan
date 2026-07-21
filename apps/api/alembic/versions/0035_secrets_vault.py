"""Secrets vault: metadata + audit ledger (Phase 68).

Sources of truth: Vol 8 §6.1–6.3 (TITAN-SEC-050/051/052/053), §3.5
(TITAN-SEC-020 signed envelopes → the ``envelope.rejected`` audit event), §10.2
(secret lifecycle is audited), SEC-092 (audit rows carry references, never
values), and docs/secrets-vault.md.

Two tables, owned by the leaf ``titan.vault`` module:

- ``secret_metadata`` — the ONLY secret facts in Postgres (SEC-050 keeps VALUES
  out of app tables; they live age-encrypted, decrypted into memory at boot).
  ``name`` is the get-by-reference key, unique across the single-tenant vault.
  ``rotated_at`` is the rotation clock, distinct from the conventions'
  ``updated_at`` so a metadata edit does not reset the 90-day window (SEC-052).
- ``secret_audit`` — append-only secret-lifecycle + envelope-integrity ledger.
  Sits beside ``security_events`` (the auth audit from 0034) rather than inside
  it because the vault is a leaf and cannot import ``titan.identity``. Carries a
  secret NAME and a payload digest, never a value (SEC-092).

Hand-written (autogenerate is forbidden); ConventionsMixin columns spelled out.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: None = None
depends_on: None = None


def _conventions() -> list[sa.Column]:
    return [
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
    ]


def _updated_at_trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def upgrade() -> None:
    op.create_table(
        "secret_metadata",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # The get-by-reference key; unique across the single-tenant vault.
        sa.Column("name", sa.Text(), nullable=False),
        # org | project | task (TITAN-SEC-052).
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_ref", sa.Text(), nullable=True),
        # SET NULL, not CASCADE: metadata + its audit trail outlive the human
        # who created the secret.
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rotation_days", sa.Integer(), nullable=False, server_default=sa.text("90")),
        # The rotation clock: expiry = rotated_at + rotation_days (SEC-052).
        sa.Column("rotated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        *_conventions(),
    )
    op.create_index("uq_secret_metadata_name", "secret_metadata", ["name"], unique=True)
    _updated_at_trigger("secret_metadata")

    op.create_table(
        "secret_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # secret.created | secret.rotated | secret.deleted | secret.injected |
        # envelope.rejected
        sa.Column("event_type", sa.Text(), nullable=False),
        # principal.sub | service name | 'system'
        sa.Column("actor", sa.Text(), nullable=False),
        # The reference, never the value (SEC-092). NULL for envelope events.
        sa.Column("secret_name", sa.Text(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        *_conventions(),
    )
    # The two axes an operator queries: this secret's history, and the recent
    # feed (envelope rejections, lifecycle). Both time-ordered.
    op.create_index("ix_secret_audit_name_time", "secret_audit", ["secret_name", "created_at"])
    op.create_index("ix_secret_audit_time", "secret_audit", ["created_at"])
    _updated_at_trigger("secret_audit")


def downgrade() -> None:
    for table in ("secret_audit", "secret_metadata"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
        op.drop_table(table)
