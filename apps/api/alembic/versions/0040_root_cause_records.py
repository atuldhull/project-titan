"""Root-Cause Records (Phase 85, Vol 10 §5.2, TITAN-EVO-012/013).

A structured five-whys analysis of a classified failure. failure_code is
CHECK-constrained to the fixed taxonomy (evolution/taxonomy/failures.yaml) — a DB
constraint proves every RCR carries exactly one valid F-* code (DoD). ``signature``
is a stable failure-signature key; a partial unique index keeps at most one OPEN
RCR per signature so recurrence reopens the existing record rather than duplicating
it. ``ep`` links the remediating Evolution Proposal (a plain string key matching
evolution_artifacts.ep); ``ep_merged`` is set when that EP lands (TITAN-EVO-013).

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0040
Revises: 0039
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_F_CODES = "'F-REQ','F-CTX','F-GEN','F-STD','F-SEC','F-ORCH','F-TOOL','F-LLM','F-HUM','F-UNK'"


def upgrade() -> None:
    op.create_table(
        "root_cause_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("failure_code", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column(
            "task_ids",
            ARRAY(UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("signature", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("five_whys", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("contributing", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("remediation", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column("ep", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("ep_merged", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        # DoD: exactly one valid F-* code from the fixed taxonomy.
        sa.CheckConstraint(f"failure_code IN ({_F_CODES})", name="ck_root_cause_records_code"),
        sa.CheckConstraint(
            "severity IN ('low','medium','high','critical')", name="ck_root_cause_records_severity"
        ),
        sa.CheckConstraint(
            "status IN ('open','remediated','accepted_risk')", name="ck_root_cause_records_status"
        ),
    )
    # At most one OPEN RCR per failure signature — a recurrence reopens it (EVO-013).
    op.create_index(
        "uq_root_cause_records_open_signature",
        "root_cause_records",
        ["signature"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_root_cause_records_status", "root_cause_records", ["status", "created_at"]
    )
    op.execute(
        "CREATE TRIGGER trg_root_cause_records_updated_at BEFORE UPDATE ON root_cause_records "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_root_cause_records_updated_at ON root_cause_records")
    op.drop_index("ix_root_cause_records_status", table_name="root_cause_records")
    op.drop_index("uq_root_cause_records_open_signature", table_name="root_cause_records")
    op.drop_table("root_cause_records")
