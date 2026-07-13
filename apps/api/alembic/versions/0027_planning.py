"""Autonomous planning mechanics (Phase 50; Vol 6 §4, TITAN-WFL-016/018/019).

Three mutable tables backing the planning domain:

* ``sprint_ceremonies`` — the planning/review/retrospective records emitted per
  sprint (WFL-016); unique on (project_id, sprint_number, kind).
* ``milestone_states`` — one current-milestone row per project (WFL-018).
* ``task_blocks`` — the blocker ledger + recovery-ladder rung reached (WFL-019);
  at most one OPEN block per task (partial unique index).

All three carry the TITAN-BCK-008 convention columns and the shared set_updated_at
trigger. Sprints and milestones already have core-schema tables (0003); this phase
adds the mechanics state, not those spines.

Hand-written (autogenerate is forbidden).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: None = None
depends_on: None = None


def _conventions() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    ]


def _updated_at_trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def upgrade() -> None:
    op.create_table(
        "sprint_ceremonies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sprint_number", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("body", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("memory_record_id", UUID(as_uuid=True), nullable=True),
        *_conventions(),
        sa.UniqueConstraint(
            "project_id",
            "sprint_number",
            "kind",
            name="uq_sprint_ceremonies_project_sprint_kind",
        ),
        sa.CheckConstraint(
            "kind IN ('planning', 'review', 'retrospective')",
            name="ck_sprint_ceremonies_kind",
        ),
    )
    _updated_at_trigger("sprint_ceremonies")

    op.create_table(
        "milestone_states",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "current_milestone", sa.Text, nullable=False, server_default=sa.text("'none'")
        ),
        sa.Column("signals", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_conventions(),
        sa.UniqueConstraint("project_id", name="uq_milestone_states_project"),
        sa.CheckConstraint(
            "current_milestone IN ('none', 'M0', 'M1', 'M2', 'M3', 'M4')",
            name="ck_milestone_states_current",
        ),
    )
    _updated_at_trigger("milestone_states")

    op.create_table(
        "task_blocks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("rung", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        sa.Column("detail", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("detected_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("resolved_at", TIMESTAMP(timezone=True), nullable=True),
        *_conventions(),
        sa.CheckConstraint(
            "status IN ('open', 'recovered', 'escalated')", name="ck_task_blocks_status"
        ),
    )
    op.create_index("ix_task_blocks_project_status", "task_blocks", ["project_id", "status"])
    # At most one ACTIVE (open or escalated) block per task — the recovery ladder
    # ratchets a single row per task; only a recovered block frees the slot.
    op.create_index(
        "uq_task_blocks_active_per_task",
        "task_blocks",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'recovered'"),
    )
    _updated_at_trigger("task_blocks")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_task_blocks_updated_at ON task_blocks")
    op.drop_index("uq_task_blocks_active_per_task", table_name="task_blocks")
    op.drop_index("ix_task_blocks_project_status", table_name="task_blocks")
    op.drop_table("task_blocks")
    op.execute("DROP TRIGGER IF EXISTS trg_milestone_states_updated_at ON milestone_states")
    op.drop_table("milestone_states")
    op.execute("DROP TRIGGER IF EXISTS trg_sprint_ceremonies_updated_at ON sprint_ceremonies")
    op.drop_table("sprint_ceremonies")
