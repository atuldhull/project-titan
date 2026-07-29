# migrations: allow-exclusive — pre-Phase-76 index on a small pre-existing table.
"""Single-owner constraint + live watchdog config (Phase 32).

Enforces TITAN-AGT-012 ("every task has exactly one owner agent at any time") in
PostgreSQL itself: a partial unique index on ``runs`` allows at most one ACTIVE
run (queued/running) per task, so two transactions racing to put two agents on
one task lose at the DB — a unique violation, not an application-code check.
Historical reassignment stays legal: terminal runs (succeeded/failed/cancelled/
timed_out) leave the index, so attempt 2 by a different agent is fine once
attempt 1 is over.

Adds ``watchdog_config`` — the Orchestrator watchdog's timeouts and thresholds
(TITAN-AGT-009/020), read fresh every sweep so a change takes effect without a
redeploy. ``project_id`` NULL is the org-wide default row; a project row
overrides it (the same COALESCE-unique pattern as ``conflict_thresholds``).

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- single active owner per task (TITAN-AGT-012) -------------------------
    op.create_index(
        "uq_runs_single_active_owner",
        "runs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    # -- try_timestamptz: exception-safe cast for model-written timestamps -----
    # A discussion's decision_deadline is a MODEL/HUMAN-written payload field; a
    # single malformed value must degrade to NULL (that row is simply not
    # expired), never abort the watchdog's whole deadline sweep.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION try_timestamptz(t text) RETURNS timestamptz AS $$
        BEGIN
            RETURN t::timestamptz;
        EXCEPTION WHEN OTHERS THEN
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql IMMUTABLE
        """
    )

    # -- watchdog_config: live-editable timeouts/thresholds (TITAN-AGT-020) ---
    op.create_table(
        "watchdog_config",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # NULL = the org-wide default row; a project row overrides it.
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        # A supervised consumer whose heartbeat is older than this is replaced.
        # A DEAD consumer (task exited/killed) is detected immediately regardless;
        # the heartbeat only catches a HUNG one, so the default exceeds the worst
        # legitimate turn (run_soft_timeout 900 s + slack) to avoid false restarts.
        sa.Column("stalled_after_ms", sa.Integer, nullable=False, server_default=sa.text("960000")),
        # How often the watchdog sweeps (restart checks + discussion deadlines).
        sa.Column("sweep_interval_ms", sa.Integer, nullable=False, server_default=sa.text("5000")),
        # Idle threshold for reclaiming pending inbox entries (crash recovery).
        # Above the worst legitimate turn, like stalled_after_ms: a routine reclaim
        # must never steal a message a live consumer is legitimately mid-turn on,
        # and the FIRST reclaim of a stuck requires_ack message must already be
        # past every type-specific ack timeout (so the escalation hook fires long
        # before the poison reaper could dead-letter it).
        sa.Column(
            "reclaim_min_idle_ms", sa.Integer, nullable=False, server_default=sa.text("960000")
        ),
        # Per-type requires_ack timeout overrides (message type -> ms); a type
        # absent here uses the code default (bus.ACK_TIMEOUTS_MS).
        sa.Column(
            "ack_timeout_overrides",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
    )
    op.create_index(
        "uq_watchdog_config_scope",
        "watchdog_config",
        [sa.text("COALESCE(project_id, '00000000-0000-0000-0000-000000000000'::uuid)")],
        unique=True,
    )
    op.execute(
        "CREATE TRIGGER trg_watchdog_config_updated_at BEFORE UPDATE ON watchdog_config "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_watchdog_config_updated_at ON watchdog_config")
    op.drop_index("uq_watchdog_config_scope", table_name="watchdog_config")
    op.drop_table("watchdog_config")
    op.execute("DROP FUNCTION IF EXISTS try_timestamptz(text)")
    op.drop_index("uq_runs_single_active_owner", table_name="runs")
