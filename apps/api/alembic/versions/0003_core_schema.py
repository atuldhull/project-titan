"""The 15-table core schema (Vol 3 §6 TITAN-BCK-008; Vol 4 §7 notifications).

Hand-written per §6 (autogenerate forbidden). Every table gets created_at,
trigger-maintained updated_at, and optimistic-lock version (via _conventions).
Statuses are text + application enums, never Postgres enums.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = [
    "users",
    "roles",
    "projects",
    "user_roles",
    "milestones",
    "sprints",
    "agents",
    "tasks",
    "task_dependencies",
    "runs",
    "artifacts",
    "messages",
    "decisions",
    "outbox",
    "notifications",
]


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
    # -- identity -----------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", CITEXT(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("auth_provider", sa.Text, nullable=False),
        sa.Column("auth_subject", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        *_conventions(),
        sa.UniqueConstraint("auth_provider", "auth_subject", name="uq_users_auth_provider"),
    )

    op.create_table(
        "roles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("permissions", JSONB, nullable=False),
        *_conventions(),
    )

    # -- projects -------------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("brief", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'intake'")),
        sa.Column("mode", sa.Text, nullable=False, server_default=sa.text("'autonomous'")),
        sa.Column("llm_profile", sa.Text, nullable=False, server_default=sa.text("'default'")),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        *_conventions(),
    )
    op.create_index("idx_projects_status", "projects", ["status"])

    # Spec deviation, documented: §6 declares PRIMARY KEY (user_id, role_id,
    # project_id) with nullable project_id, which Postgres forbids. Same
    # semantics via surrogate PK + UNIQUE NULLS NOT DISTINCT.
    op.create_table(
        "user_roles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            UUID(as_uuid=True),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        *_conventions(),
        sa.UniqueConstraint(
            "user_id",
            "role_id",
            "project_id",
            name="uq_user_roles_user_id",
            postgresql_nulls_not_distinct=True,
        ),
    )

    op.create_table(
        "milestones",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("due_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'open'")),
        *_conventions(),
    )

    op.create_table(
        "sprints",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("goal", sa.Text, nullable=True),
        sa.Column("starts_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ends_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'planned'")),
        *_conventions(),
        sa.UniqueConstraint("project_id", "number", name="uq_sprints_project_id"),
    )

    # -- agents ---------------------------------------------------------------
    op.create_table(
        "agents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'idle'")),
        sa.Column("model_binding", JSONB, nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # plain uuid on purpose — an FK to tasks would be circular (§6)
        sa.Column("current_task_id", UUID(as_uuid=True), nullable=True),
        *_conventions(),
    )
    op.create_index("idx_agents_project_role", "agents", ["project_id", "role"])

    # -- orchestration ----------------------------------------------------------
    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sprint_id", UUID(as_uuid=True), sa.ForeignKey("sprints.id"), nullable=True),
        sa.Column(
            "milestone_id", UUID(as_uuid=True), sa.ForeignKey("milestones.id"), nullable=True
        ),
        sa.Column("parent_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("spec", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'backlog'")),
        sa.Column("priority", sa.SmallInteger, nullable=False, server_default=sa.text("3")),
        sa.Column("estimate_points", sa.SmallInteger, nullable=True),
        sa.Column(
            "assignee_agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True
        ),
        sa.Column(
            "reviewer_agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True
        ),
        sa.Column(
            "created_by_agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_conventions(),
    )
    op.create_index(
        "idx_tasks_project_status_priority", "tasks", ["project_id", "status", "priority"]
    )
    op.create_index(
        "idx_tasks_assignee",
        "tasks",
        ["assignee_agent_id"],
        postgresql_where=sa.text("status IN ('assigned', 'in_progress')"),
    )

    op.create_table(
        "task_dependencies",
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "depends_on_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        *_conventions(),
        sa.CheckConstraint(
            "task_id <> depends_on_id", name="ck_task_dependencies_no_self_dependency"
        ),
    )

    # -- execution --------------------------------------------------------------
    op.create_table(
        "runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'queued'")),
        sa.Column("input", JSONB, nullable=False),
        sa.Column("output_summary", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("provider", sa.Text, nullable=True),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("tokens_in", sa.Integer, server_default=sa.text("0")),
        sa.Column("tokens_out", sa.Integer, server_default=sa.text("0")),
        sa.Column("cost_usd", sa.Numeric(10, 4), server_default=sa.text("0")),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_conventions(),
    )
    op.create_index("idx_runs_task", "runs", ["task_id", sa.text("attempt DESC")])
    op.create_index(
        "idx_runs_status",
        "runs",
        ["status"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=True),
        sa.Column("storage_ref", sa.Text, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        *_conventions(),
    )
    op.create_index("idx_artifacts_project_kind", "artifacts", ["project_id", "kind"])

    # -- communication ------------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("thread_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("sender_agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True),
        sa.Column("sender_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("body", JSONB, nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=True),
        *_conventions(),
        sa.CheckConstraint(
            "sender_agent_id IS NOT NULL OR sender_user_id IS NOT NULL",
            name="ck_messages_sender_present",
        ),
    )
    op.create_index("idx_messages_thread", "messages", ["thread_id", "created_at"])
    op.create_index(
        "idx_messages_project_kind",
        "messages",
        ["project_id", "kind", sa.text("created_at DESC")],
    )

    op.create_table(
        "decisions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("context", sa.Text, nullable=False),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("alternatives", sa.Text, nullable=False),
        sa.Column("consequences", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'accepted'")),
        sa.Column(
            "decided_by_agent_id", UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=True
        ),
        sa.Column(
            "supersedes_id", UUID(as_uuid=True), sa.ForeignKey("decisions.id"), nullable=True
        ),
        *_conventions(),
    )
    op.create_index(
        "idx_decisions_project", "decisions", ["project_id", sa.text("created_at DESC")]
    )

    # -- events (outbox) ------------------------------------------------------------
    op.create_table(
        "outbox",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("topic", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_conventions(),
    )
    op.create_index(
        "idx_outbox_unpublished",
        "outbox",
        ["created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    # -- notifications (15th table; Vol 4 §7, TITAN-FRT-038) -------------------------
    op.create_table(
        "notifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=True),
        sa.Column("link", sa.Text, nullable=True),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_conventions(),
    )
    op.create_index(
        "idx_notifications_unread",
        "notifications",
        ["user_id"],
        postgresql_where=sa.text("read_at IS NULL"),
    )

    # -- updated_at triggers on every table (TITAN-BCK-008) --------------------------
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
