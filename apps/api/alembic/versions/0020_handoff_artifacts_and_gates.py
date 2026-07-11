"""Handoff artifacts + the auditable gate ledger (Vol 6 §2.2–2.3,
TITAN-WFL-003/004/005).

Phase 42 makes each Phase-41 pipeline stage transition a §2.2 handoff:

- ``handoff_artifacts`` — the versioned, schema-validated §2.3 artifact each
  producing stage emits, stored as JSONB with its artifact_type + schema_version
  and a per-(project, stage) monotonic ``version`` (re-submissions bump it). This
  is the "artifacts table" the handoff contract stores into.
- ``gate_evaluations`` — the TITAN-WFL-004 gate ledger: one append-only row per
  gate evaluation with verdict, evaluator identity (agent + model + prompt
  version), reasons (field-level errors on a schema failure), attempt number and
  timestamp. A complete pass/fail ledger for any release exports from here.
  Append-only via the shared ``reject_decision_mutation()`` trigger (from 0011).
- ``pipeline_states.retry_budget`` — the TITAN-WFL-005 per-project retry budget
  (default 3); on exhaustion the executor escalates the project to NEEDS_HUMAN.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- TITAN-WFL-003: the versioned, schema-validated artifact store ----------
    op.create_table(
        "handoff_artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("artifact_type", sa.Text, nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default=sa.text("1")),
        # Per-(project, stage) monotonic version — a re-submission is a new version.
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("body", JSONB, nullable=False),
        sa.Column("submitted_by", sa.Text, nullable=False),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint(
            "project_id", "stage", "version", name="uq_handoff_artifacts_project_stage_version"
        ),
    )
    op.create_index(
        "ix_handoff_artifacts_project_id", "handoff_artifacts", ["project_id", "stage"]
    )

    # -- TITAN-WFL-004: the append-only gate ledger ----------------------------
    op.create_table(
        "gate_evaluations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text, nullable=False),
        # Null when schema validation failed before a valid artifact was stored.
        sa.Column(
            "artifact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("handoff_artifacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attempt", sa.Integer, nullable=False),
        # pass | fail
        sa.Column("verdict", sa.Text, nullable=False),
        sa.Column("reasons", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        # Evaluator identity (TITAN-WFL-004): agent + model + prompt version.
        sa.Column("evaluator_agent", sa.Text, nullable=False),
        sa.Column("evaluator_model", sa.Text, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index(
        "ix_gate_evaluations_project_id", "gate_evaluations", ["project_id", "stage", "id"]
    )
    # Append-only (TITAN-AGT-019 / TITAN-WFL-004): a gate ledger cannot be rewritten.
    op.execute(
        "CREATE TRIGGER trg_gate_evaluations_append_only "
        "BEFORE UPDATE OR DELETE ON gate_evaluations "
        "FOR EACH ROW EXECUTE FUNCTION reject_decision_mutation()"
    )

    # -- TITAN-WFL-005: per-project retry budget -------------------------------
    op.add_column(
        "pipeline_states",
        sa.Column("retry_budget", sa.Integer, nullable=False, server_default=sa.text("3")),
    )


def downgrade() -> None:
    op.drop_column("pipeline_states", "retry_budget")
    op.execute("DROP TRIGGER IF EXISTS trg_gate_evaluations_append_only ON gate_evaluations")
    op.drop_index("ix_gate_evaluations_project_id", table_name="gate_evaluations")
    op.drop_table("gate_evaluations")
    op.drop_index("ix_handoff_artifacts_project_id", table_name="handoff_artifacts")
    op.drop_table("handoff_artifacts")
