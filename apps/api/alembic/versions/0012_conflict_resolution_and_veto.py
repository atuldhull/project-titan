"""Conflict-resolution state machine, security-veto gate, deadlock config (Phase 31).

Persists the §7 conflict flow so it is enforced by the runtime, never by prompt
compliance (TITAN-AGT-017): ``conflicts`` holds each dispute's CURRENT state
(direct → arbitration → executive → resolved) and ``conflict_transitions`` is the
append-only audit of every move (an UPDATE/DELETE is rejected by a trigger, like
``decisions``). ``merge_vetoes`` records a Security veto against an artifact so the
pipeline can block a merge/release regardless of any other approval, including the
CEO's (TITAN-AGT-018); an override is expressed by a permanent ``decisions`` row
with ``authority = 'human_override'`` and the veto flipping to ``overridden``.
``conflict_thresholds`` holds the deadlock thresholds (rule 5) in the DB so a change
takes effect on the next read — no redeploy (TITAN-AGT-020).

Also adds ``decisions.authority`` so a resolution records the authority that made it
(decides_alone | arbitration | executive | human_override) as a first-class,
append-only fact (TITAN-AGT-019).

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0012"
down_revision: str | None = "0011"
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
    # -- decisions.authority: the authority that produced a decision -----------
    # decides_alone | arbitration | executive | human_override (TITAN-AGT-019).
    op.add_column("decisions", sa.Column("authority", sa.Text, nullable=True))

    # -- conflicts: the persisted §7 state machine (current state) -------------
    op.create_table(
        "conflicts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The §6.2 ConflictResolution.conflict_id — the natural key agents thread on.
        sa.Column("conflict_id", UUID(as_uuid=True), nullable=False),
        # technical | scope | priority | cross_domain — drives arbiter appointment.
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("parties", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        # direct | arbitration | executive | resolved (§7 rules 1–3).
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'direct'")),
        sa.Column("round", sa.Integer, nullable=False, server_default=sa.text("1")),
        # The appointed arbiter role (architect | pm_product | pm_project), or null.
        sa.Column("arbiter", sa.Text, nullable=True),
        sa.Column("opened_by", sa.Text, nullable=False),
        # Set once a deadlock (rule 5) auto-escalated this conflict.
        sa.Column("deadlock_flagged", sa.Boolean, nullable=False, server_default=sa.text("false")),
        # The binding DecisionLog that resolved it (arbitration/executive/human).
        sa.Column(
            "resolution_decision_id",
            UUID(as_uuid=True),
            sa.ForeignKey("decisions.id"),
            nullable=True,
        ),
        *_conventions(),
    )
    # One live conflict per (project, conflict_id) — reopening threads the same id.
    op.create_index(
        "uq_conflicts_project_cid", "conflicts", ["project_id", "conflict_id"], unique=True
    )
    op.execute(
        "CREATE TRIGGER trg_conflicts_updated_at BEFORE UPDATE ON conflicts "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # -- conflict_transitions: append-only audit of every state move -----------
    op.create_table(
        "conflict_transitions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conflict_row_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conflicts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("from_state", sa.Text, nullable=True),  # null on the opening row
        sa.Column("to_state", sa.Text, nullable=False),
        # opened | no_consensus | cross_domain | arbiter_verdict | disputed |
        # executive_decision | deadlock_escalation
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),  # role / orchestrator / ceo / human
        sa.Column("round", sa.Integer, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        *_conventions(),
    )
    op.create_index(
        "idx_conflict_transitions_conflict",
        "conflict_transitions",
        ["conflict_row_id", "created_at"],
    )
    # Append-only (TITAN-AGT-019): the audit of a state machine cannot be rewritten.
    # Reuses the shared reject_decision_mutation() function from 0011; the name is
    # generic ("... rejected"), so it serves any append-only table.
    op.execute(
        "CREATE TRIGGER trg_conflict_transitions_append_only "
        "BEFORE UPDATE OR DELETE ON conflict_transitions "
        "FOR EACH ROW EXECUTE FUNCTION reject_decision_mutation()"
    )

    # -- merge_vetoes: the security-veto pipeline gate (TITAN-AGT-018) ----------
    op.create_table(
        "merge_vetoes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_ref", sa.Text, nullable=False),  # e.g. "pr:42" / "release:1.2.0"
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("raised_by", sa.Text, nullable=False),  # agent role (security)
        # The ReviewRequest-reply message carrying the veto verdict, for audit.
        sa.Column(
            "veto_message_id", UUID(as_uuid=True), sa.ForeignKey("messages.id"), nullable=True
        ),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
        # The permanent human_override DecisionLog that lifted it, if any.
        sa.Column(
            "override_decision_id",
            UUID(as_uuid=True),
            sa.ForeignKey("decisions.id"),
            nullable=True,
        ),
        *_conventions(),
    )
    # At most one ACTIVE veto per artifact — re-vetoing is idempotent; an overridden
    # veto no longer occupies the slot, so a later finding can re-block.
    op.create_index(
        "uq_merge_vetoes_active",
        "merge_vetoes",
        ["project_id", "artifact_ref"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "idx_merge_vetoes_task",
        "merge_vetoes",
        ["task_id"],
        postgresql_where=sa.text("task_id IS NOT NULL AND status = 'active'"),
    )
    op.execute(
        "CREATE TRIGGER trg_merge_vetoes_updated_at BEFORE UPDATE ON merge_vetoes "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    # -- conflict_thresholds: deadlock thresholds, live-editable (TITAN-AGT-020)-
    op.create_table(
        "conflict_thresholds",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # null = the org-wide default row; a project row overrides it.
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("max_conflict_rounds", sa.Integer, nullable=False, server_default=sa.text("3")),
        sa.Column("max_task_reopens", sa.Integer, nullable=False, server_default=sa.text("3")),
        *_conventions(),
    )
    # One row per project, and a single global-default row (project_id NULL). The
    # COALESCE expression index makes NULL a normal key so the default is unique too.
    op.create_index(
        "uq_conflict_thresholds_scope",
        "conflict_thresholds",
        [sa.text("COALESCE(project_id, '00000000-0000-0000-0000-000000000000'::uuid)")],
        unique=True,
    )
    op.execute(
        "CREATE TRIGGER trg_conflict_thresholds_updated_at BEFORE UPDATE ON conflict_thresholds "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_conflict_thresholds_updated_at ON conflict_thresholds")
    op.drop_table("conflict_thresholds")
    op.execute("DROP TRIGGER IF EXISTS trg_merge_vetoes_updated_at ON merge_vetoes")
    op.drop_table("merge_vetoes")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_conflict_transitions_append_only ON conflict_transitions"
    )
    op.drop_table("conflict_transitions")
    op.execute("DROP TRIGGER IF EXISTS trg_conflicts_updated_at ON conflicts")
    op.drop_table("conflicts")
    op.drop_column("decisions", "authority")
