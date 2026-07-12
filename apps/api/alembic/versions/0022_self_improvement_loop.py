"""The self-improvement loop engine (Vol 6 §3, TITAN-WFL-010–014).

Every engineering task runs a bounded per-task micro-loop before it may MERGE — no
component ships as a first draft. Phase 44 adds:

- ``loop_records`` — one loop per task (UNIQUE task_id): its cycle count, the
  configurable cap (2–5, default 3), status, and termination reason (converged |
  exhausted). A task cannot reach MERGED without an approved/converged loop record
  (TITAN-WFL-010, enforced as an orchestrator gate on the Phase-41 task machine).
- ``loop_cycles`` — one append-only row per completed cycle: how many NEW findings
  of severity ≥ minor it raised and whether it was clean, so convergence (two
  consecutive clean cycles) is logged distinctly from exhaustion (TITAN-WFL-013).
- ``loop_findings`` — the review findings with their refute-or-fix disposition
  (TITAN-WFL-012). ``refute_upheld_count`` drives the twice-refuted → Architect
  arbitration route; ``fingerprint`` (UNIQUE per loop) makes "new finding" and
  oscillation deterministic; ``conflict_id`` links a routed arbitration.
- ``loop_stage_events`` — the append-only audit ledger of every stage execution.
  The REVIEW row records the reviewer's ISOLATED context manifest (which sections
  it received — never implementation chain-of-thought), the TITAN-WFL-011 audit.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import BIGINT, JSONB, TIMESTAMP, UUID

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _conventions() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    ]


def upgrade() -> None:
    # -- TITAN-WFL-010: one loop per task, the MERGED gate's record --------------
    op.create_table(
        "loop_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # running | converged | escalated | arbitration | superseded
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'running'")),
        # Monotonic per task: a reopened task runs a FRESH loop (new iteration) so a
        # stale converged record can never let re-worked code merge (TITAN-WFL-010).
        sa.Column("iteration", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("cycle_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("max_cycles", sa.Integer, nullable=False, server_default=sa.text("3")),
        # converged | exhausted (null while running)
        sa.Column("termination_reason", sa.Text, nullable=True),
        *_conventions(),
        sa.UniqueConstraint("task_id", "iteration", name="uq_loop_records_task_iteration"),
    )
    op.execute(
        "CREATE TRIGGER trg_loop_records_updated_at BEFORE UPDATE ON loop_records "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # -- TITAN-WFL-013: one append-only row per completed cycle ------------------
    op.create_table(
        "loop_cycles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loop_id",
            UUID(as_uuid=True),
            sa.ForeignKey("loop_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cycle_number", sa.Integer, nullable=False),
        sa.Column("new_findings_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("clean", sa.Boolean, nullable=False, server_default=sa.text("false")),
        # continue | converged | exhausted | arbitration
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("loop_id", "cycle_number", name="uq_loop_cycles_loop_number"),
    )
    op.execute(
        "CREATE TRIGGER trg_loop_cycles_append_only BEFORE UPDATE OR DELETE ON loop_cycles "
        "FOR EACH ROW EXECUTE FUNCTION reject_decision_mutation()"
    )

    # -- TITAN-WFL-012: findings + refute-or-fix disposition --------------------
    op.create_table(
        "loop_findings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loop_id",
            UUID(as_uuid=True),
            sa.ForeignKey("loop_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("raised_cycle", sa.Integer, nullable=False),
        # info | minor | major | blocking
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("file", sa.Text, nullable=False),
        sa.Column("line", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("suggestion", sa.Text, nullable=False, server_default=sa.text("''")),
        # A stable per-loop identity (severity+file+line+rule) — makes "new finding"
        # and oscillation deterministic; re-asserting the same finding is a no-op.
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("hunk_key", sa.Text, nullable=False, server_default=sa.text("''")),
        # open | fixed | refuted | upheld | arbitration
        sa.Column("disposition", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column("refutation_reason", sa.Text, nullable=True),
        # Times the engineer refuted AND the reviewer upheld — 2 routes to arbitration.
        sa.Column("refute_upheld_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        # The conflict-machine dispute id, if this finding routed to arbitration.
        sa.Column("conflict_id", UUID(as_uuid=True), nullable=True),
        *_conventions(),
        sa.UniqueConstraint("loop_id", "fingerprint", name="uq_loop_findings_loop_fingerprint"),
    )
    op.create_index("ix_loop_findings_loop", "loop_findings", ["loop_id"])
    op.execute(
        "CREATE TRIGGER trg_loop_findings_updated_at BEFORE UPDATE ON loop_findings "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # -- TITAN-WFL-011: append-only stage-event audit ledger --------------------
    op.create_table(
        "loop_stage_events",
        sa.Column("id", BIGINT, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "loop_id",
            UUID(as_uuid=True),
            sa.ForeignKey("loop_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", UUID(as_uuid=True), nullable=False),
        sa.Column("cycle_number", sa.Integer, nullable=False),
        sa.Column("stage", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        # pass | fail | n/a
        sa.Column("verdict", sa.Text, nullable=False, server_default=sa.text("'n/a'")),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_loop_stage_events_loop", "loop_stage_events", ["loop_id", "id"])
    op.execute(
        "CREATE TRIGGER trg_loop_stage_events_append_only "
        "BEFORE UPDATE OR DELETE ON loop_stage_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_decision_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_loop_stage_events_append_only ON loop_stage_events")
    op.drop_index("ix_loop_stage_events_loop", table_name="loop_stage_events")
    op.drop_table("loop_stage_events")
    op.execute("DROP TRIGGER IF EXISTS trg_loop_findings_updated_at ON loop_findings")
    op.drop_index("ix_loop_findings_loop", table_name="loop_findings")
    op.drop_table("loop_findings")
    op.execute("DROP TRIGGER IF EXISTS trg_loop_cycles_append_only ON loop_cycles")
    op.drop_table("loop_cycles")
    op.execute("DROP TRIGGER IF EXISTS trg_loop_records_updated_at ON loop_records")
    op.drop_table("loop_records")
