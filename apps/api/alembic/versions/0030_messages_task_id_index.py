# migrations: allow-exclusive — pre-Phase-76 index on a small pre-existing table.
"""Index messages by task, for the Kanban task Sheet (Phase 59; Vol 4 §6.3).

``messages.task_id`` has been a real FK since 0003, and every task state change
writes a transition message against it (``orchestrator.service.
_write_transition_message``) — so the task's event history and its conversation
were both already durable, just unreadable: ``GET /projects/{id}/messages``
offered no ``task_id`` filter, and the column carried no index.

Phase 59 adds the filter. This adds the index it needs: without it, opening one
task's Sheet seq-scans the project's entire message log.

Composite on (project_id, task_id): the filter is always project-scoped (the
route nests under /projects/{project_id}), so a lone task_id index would leave
the project predicate to a filter step. Partial on task_id IS NOT NULL — most
messages are project chatter with no task, and they can never match this query.

Hand-written (autogenerate is forbidden).
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: None = None
depends_on: None = None

_INDEX = "idx_messages_project_task"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "messages",
        ["project_id", "task_id"],
        postgresql_where=sa.text("task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="messages")
