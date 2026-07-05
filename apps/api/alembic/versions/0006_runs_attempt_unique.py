"""Unique (task_id, attempt) on runs — the durable guarantee behind the §8.2
'attempt = max+1' invariant (Phase 12 review).

Run creation also serializes per task on a transaction advisory lock, so this
constraint is a declarative backstop that also defends any future write path
that forgets to take the lock: two runs of one task can never share an attempt.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_runs_task_id_attempt", "runs", ["task_id", "attempt"])


def downgrade() -> None:
    op.drop_constraint("uq_runs_task_id_attempt", "runs", type_="unique")
