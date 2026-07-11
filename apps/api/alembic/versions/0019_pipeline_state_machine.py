"""Per-project delivery-pipeline state machine (Vol 6 §2.1, TITAN-WFL-001/002).

Phase 41 opens M5 — Autonomous delivery. Adds the two tables the 12-stage
pipeline needs:

- ``pipeline_states`` — one row per project holding the current stage + the
  iteration count of that stage (TITAN-WFL-001). Because the authoritative
  cursor lives in PostgreSQL, killing and restarting the orchestrator resumes
  the project at the exact stage and iteration (the advancement loop is
  stateless — it reads this row every tick).
- ``pipeline_stage_events`` — the append-only stage ledger: one row per stage
  transition (enter / stage_complete / rejected / not_applicable / needs_human /
  released / budget_hold / resumed). This is the durable event log the acceptance
  criteria require — "the event log reconstructs the full history" — and the
  foundation the Phase-42 gate ledger builds on. Append-only at the DB layer via
  the shared ``reject_decision_mutation()`` trigger function (from 0011), like the
  ``decisions`` / ``conflict_transitions`` audits (TITAN-AGT-019).

The Volume-6 §2.5 task states (IN_LOOP / MERGED / BLOCKED / FAILED / ESCALATED)
need no DDL: ``tasks.status`` is free TEXT (Vol 3 §6 — statuses are text + Pydantic
enums, never Postgres enums), so the extended transition table just uses new
values. Likewise budget_hold reuses the existing ``budget_holds`` table (0007) —
it is an orthogonal, resumable overlay, never a task status.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import BIGINT, JSONB, TIMESTAMP, UUID

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _conventions() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    # -- TITAN-WFL-001: the per-project pipeline cursor ------------------------
    # project_id is the PK: exactly one pipeline per project. current_stage +
    # iteration ARE the resumable state; status is the coarse lifecycle
    # (running | needs_human | released).
    op.create_table(
        "pipeline_states",
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "current_stage", sa.Text, nullable=False, server_default=sa.text("'ceo_vision'")
        ),
        sa.Column("iteration", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'running'")),
        *_conventions(),
    )
    op.execute(
        "CREATE TRIGGER trg_pipeline_states_updated_at BEFORE UPDATE ON pipeline_states "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # -- TITAN-WFL-001/002: the append-only stage ledger (full history) --------
    op.create_table(
        "pipeline_stage_events",
        sa.Column("id", BIGINT, sa.Identity(always=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.Text, nullable=False),
        # enter | stage_complete | rejected | not_applicable | needs_human |
        # released | budget_hold | resumed
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("iteration", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("actor", sa.Text, nullable=False),
        # justification (NOT_APPLICABLE), rejection reason, target stage, etc.
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    # History replay + the "reconstruct the full history" check read in insert
    # order per project.
    op.create_index(
        "ix_pipeline_stage_events_project_id",
        "pipeline_stage_events",
        ["project_id", "id"],
    )
    # Append-only (TITAN-AGT-019 / TITAN-WFL-004 foundation): the audit of a state
    # machine cannot be rewritten. Reuses the generic reject_decision_mutation()
    # from 0011.
    op.execute(
        "CREATE TRIGGER trg_pipeline_stage_events_append_only "
        "BEFORE UPDATE OR DELETE ON pipeline_stage_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_decision_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_pipeline_stage_events_append_only ON pipeline_stage_events"
    )
    op.drop_index("ix_pipeline_stage_events_project_id", table_name="pipeline_stage_events")
    op.drop_table("pipeline_stage_events")
    op.execute("DROP TRIGGER IF EXISTS trg_pipeline_states_updated_at ON pipeline_states")
    op.drop_table("pipeline_states")
