"""Agent-message envelope columns + DecisionLog append-only policy (Phase 27).

Extends ``messages`` into the full §6.1 ``AgentMessage`` envelope (schema_version,
correlation_id, from_agent/to_agent role ids, priority, requires_ack) so the bus
persists every field before enqueue (TITAN-AGT-016), and relaxes the sender check
so an agent-authored message identifies its sender by role (from_agent) rather
than a runtime agent row. Adds a DB-level append-only policy on ``decisions``
(TITAN-AGT-019): UPDATE and DELETE are rejected by a trigger, not just in app
code — supersession is expressed by a new row's supersedes_id and derived, never
by mutating history.

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- messages: the §6.1 envelope columns ---------------------------------
    op.add_column(
        "messages",
        sa.Column("schema_version", sa.Text, nullable=False, server_default=sa.text("'1.0'")),
    )
    op.add_column("messages", sa.Column("correlation_id", UUID(as_uuid=True), nullable=True))
    op.add_column("messages", sa.Column("from_agent", sa.Text, nullable=True))
    op.add_column("messages", sa.Column("to_agent", sa.Text, nullable=True))
    op.add_column(
        "messages",
        sa.Column("priority", sa.Text, nullable=False, server_default=sa.text("'normal'")),
    )
    op.add_column(
        "messages",
        sa.Column("requires_ack", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    # An agent-bus message is sent by a role (from_agent); a human/REST message
    # by a user. Either identifies a sender — so the check now admits from_agent.
    op.drop_constraint("ck_messages_sender_present", "messages", type_="check")
    op.create_check_constraint(
        "ck_messages_sender_present",
        "messages",
        "sender_agent_id IS NOT NULL OR sender_user_id IS NOT NULL OR from_agent IS NOT NULL",
    )
    # Reply pairing (§6.2) + per-agent inbox replay lookups (§6.3).
    op.create_index(
        "idx_messages_correlation",
        "messages",
        ["correlation_id"],
        postgresql_where=sa.text("correlation_id IS NOT NULL"),
    )
    op.create_index(
        "idx_messages_to_agent",
        "messages",
        ["project_id", "to_agent", "created_at"],
        postgresql_where=sa.text("to_agent IS NOT NULL"),
    )

    # -- decisions: append-only at the DB layer (TITAN-AGT-019) ---------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_decision_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'decisions are append-only (TITAN-AGT-019): % rejected',
                TG_OP USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # Fires before set_updated_at (alphabetical) and aborts the statement, so no
    # UPDATE/DELETE against a decision row can ever succeed.
    op.execute(
        "CREATE TRIGGER trg_decisions_append_only BEFORE UPDATE OR DELETE ON decisions "
        "FOR EACH ROW EXECUTE FUNCTION reject_decision_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_decisions_append_only ON decisions")
    op.execute("DROP FUNCTION IF EXISTS reject_decision_mutation()")
    op.drop_index("idx_messages_to_agent", table_name="messages")
    op.drop_index("idx_messages_correlation", table_name="messages")
    op.drop_constraint("ck_messages_sender_present", "messages", type_="check")
    # Reverting the envelope removes the "sender by role" concept: agent-bus
    # messages (from_agent set, no user/agent-row sender) cannot exist under the
    # old constraint, so drop them before re-tightening it.
    op.execute("DELETE FROM messages WHERE sender_agent_id IS NULL AND sender_user_id IS NULL")
    op.create_check_constraint(
        "ck_messages_sender_present",
        "messages",
        "sender_agent_id IS NOT NULL OR sender_user_id IS NOT NULL",
    )
    for col in (
        "requires_ack",
        "priority",
        "to_agent",
        "from_agent",
        "correlation_id",
        "schema_version",
    ):
        op.drop_column("messages", col)
