# migrations: allow-exclusive — pre-Phase-76 index on a small pre-existing table.
"""Index an agent's activity: its runs and its messages (Phase 61; Vol 4 §6.5).

Vol 4 §6.5 renders one card per agent showing its current task, the model and
provider in use, its token throughput, its elapsed time and its error state.
None of that is on the ``agents`` table: ``current_task_id`` has no writer
anywhere in the codebase (it is always NULL) and ``status`` only ever holds
``idle`` or ``paused``. All of it *is* on ``runs``, which carries a NOT NULL
``agent_id`` FK alongside task_id, status, provider, model, tokens_in/out,
started_at, finished_at and error.

So the agent grid reads runs, and this indexes the three filters that makes
possible:

1. ``idx_runs_agent`` — ``runs.agent_id`` is an FK that has never had an index
   (0006 indexed task and status only). It backs the ``?agent_id=`` filter on
   ``GET /projects/{id}/runs``: the only way to ask "what has this agent done",
   and without an index it seq-scans every run ever executed. (The grid itself
   fetches a project's runs once and buckets them per agent client-side, so it
   does not use the filter — the API capability and its index stand on their own
   merits, not on that screen.)

2. ``idx_messages_project_from_agent`` — where an agent's output actually IS,
   and what the §6.5 drawer reads. Every message an agent emits during a turn
   goes through ``bus.send``, which writes ``sender_agent_id=None`` on purpose
   ("bus messages identify their sender by role") and records the sender as a
   ROLE in ``from_agent``. Mirrors the treatment ``to_agent`` — the other end of
   the same conversation — already has (idx_messages_to_agent, 0003).

3. ``idx_messages_project_sender_agent`` — the narrower half: ``sender_agent_id``
   is populated ONLY by actor-driven transitions, which pass the acting agent's
   id through. Precisely attributed but partial, so it backs the ``?sender_agent_id=``
   filter rather than the drawer. Composite because the filter is always
   project-scoped (the route nests under /projects/{project_id}); partial
   because a human's messages carry sender_user_id and can never match it.

All three are plain additive indexes: no column, constraint or data changes.
Hand-written (autogenerate is forbidden).
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: None = None
depends_on: None = None

_RUNS_AGENT = "idx_runs_agent"
_MESSAGES_SENDER = "idx_messages_project_sender_agent"
_MESSAGES_FROM = "idx_messages_project_from_agent"


def upgrade() -> None:
    # created_at DESC mirrors the keyset order every list endpoint sorts by, so
    # an agent's newest runs come off the index in order rather than needing a
    # sort step.
    op.create_index(
        _RUNS_AGENT,
        "runs",
        ["agent_id", sa.text("created_at DESC")],
    )
    op.create_index(
        _MESSAGES_SENDER,
        "messages",
        ["project_id", "sender_agent_id"],
        postgresql_where=sa.text("sender_agent_id IS NOT NULL"),
    )
    # Mirrors idx_messages_to_agent (0003), which indexes the other end of the
    # same conversation.
    op.create_index(
        _MESSAGES_FROM,
        "messages",
        ["project_id", "from_agent", sa.text("created_at DESC")],
        postgresql_where=sa.text("from_agent IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_MESSAGES_FROM, table_name="messages")
    op.drop_index(_MESSAGES_SENDER, table_name="messages")
    op.drop_index(_RUNS_AGENT, table_name="runs")
