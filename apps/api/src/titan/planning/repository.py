"""Private data access for the planning domain. SQLAlchemy Core over an
``AsyncConnection``; only ``service`` is importable across the module boundary.

The blocker sweep reads a small read-model that joins the orchestrator task graph
with two cross-domain signals (an open clarification batch; an escalated loop
record). These are stable status columns; the query is a read model, never a write
into another domain."""

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7

# --- sprint ceremonies -------------------------------------------------------

_CEREMONY_COLS = (
    "id, project_id, sprint_number, kind, body, memory_record_id, created_at, updated_at, version"
)


async def upsert_ceremony(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    sprint_number: int,
    kind: str,
    body: dict[str, Any],
    memory_record_id: uuid.UUID | None,
) -> Row[Any]:
    """Get-or-replace the ``(project_id, sprint_number, kind)`` ceremony record."""
    result = await conn.execute(
        text(
            "INSERT INTO sprint_ceremonies "
            "(id, project_id, sprint_number, kind, body, memory_record_id) "
            "VALUES (:id, :pid, :num, :kind, CAST(:body AS jsonb), :mrid) "
            "ON CONFLICT (project_id, sprint_number, kind) DO UPDATE SET "
            "body = EXCLUDED.body, memory_record_id = EXCLUDED.memory_record_id, "
            "version = sprint_ceremonies.version + 1 "
            f"RETURNING {_CEREMONY_COLS}"
        ),
        {
            "id": uuid7(),
            "pid": project_id,
            "num": sprint_number,
            "kind": kind,
            "body": json.dumps(body),
            "mrid": memory_record_id,
        },
    )
    return result.one()


async def list_ceremonies(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(
            f"SELECT {_CEREMONY_COLS} FROM sprint_ceremonies WHERE project_id = :pid "
            "ORDER BY sprint_number, kind"
        ),
        {"pid": project_id},
    )
    return result.all()


# --- milestone state ---------------------------------------------------------

_MILESTONE_COLS = "id, project_id, current_milestone, signals, created_at, updated_at, version"


async def get_milestone_state(conn: AsyncConnection, project_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_MILESTONE_COLS} FROM milestone_states WHERE project_id = :pid"),
        {"pid": project_id},
    )
    return result.one_or_none()


async def upsert_milestone_state(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    *,
    current_milestone: str,
    signals: dict[str, Any],
) -> Row[Any]:
    result = await conn.execute(
        text(
            "INSERT INTO milestone_states (id, project_id, current_milestone, signals) "
            "VALUES (:id, :pid, :cur, CAST(:sig AS jsonb)) "
            "ON CONFLICT (project_id) DO UPDATE SET "
            "current_milestone = EXCLUDED.current_milestone, signals = EXCLUDED.signals, "
            "version = milestone_states.version + 1 "
            f"RETURNING {_MILESTONE_COLS}"
        ),
        {"id": uuid7(), "pid": project_id, "cur": current_milestone, "sig": json.dumps(signals)},
    )
    return result.one()


# --- task blocks -------------------------------------------------------------

_BLOCK_COLS = (
    "id, project_id, task_id, reason, rung, status, detail, detected_at, resolved_at, "
    "created_at, updated_at, version"
)


async def get_active_block(conn: AsyncConnection, task_id: uuid.UUID) -> Row[Any] | None:
    """The task's unresolved block (status open or escalated), if any. At most one
    exists (partial unique index)."""
    result = await conn.execute(
        text(
            f"SELECT {_BLOCK_COLS} FROM task_blocks WHERE task_id = :tid AND status <> 'recovered'"
        ),
        {"tid": task_id},
    )
    return result.one_or_none()


async def insert_block(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    *,
    reason: str,
    rung: str,
    detail: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            "INSERT INTO task_blocks (id, project_id, task_id, reason, rung, detail) "
            "VALUES (:id, :pid, :tid, :reason, :rung, :detail) "
            f"RETURNING {_BLOCK_COLS}"
        ),
        {
            "id": uuid7(),
            "pid": project_id,
            "tid": task_id,
            "reason": reason,
            "rung": rung,
            "detail": detail,
        },
    )
    return result.one()


async def update_block(
    conn: AsyncConnection,
    block_id: uuid.UUID,
    *,
    reason: str,
    rung: str,
    status: str,
    detail: str,
    resolve: bool,
) -> Row[Any]:
    result = await conn.execute(
        text(
            "UPDATE task_blocks SET reason = :reason, rung = :rung, status = :status, "
            "detail = :detail, resolved_at = CASE WHEN :resolve THEN now() ELSE resolved_at END, "
            "version = version + 1 "
            f"WHERE id = :id RETURNING {_BLOCK_COLS}"
        ),
        {
            "id": block_id,
            "reason": reason,
            "rung": rung,
            "status": status,
            "detail": detail,
            "resolve": resolve,
        },
    )
    return result.one()


async def list_blocks(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(
            f"SELECT {_BLOCK_COLS} FROM task_blocks WHERE project_id = :pid "
            "ORDER BY detected_at DESC"
        ),
        {"pid": project_id},
    )
    return result.all()


_SWEEP_QUERY = text(
    "SELECT t.id AS task_id, t.project_id, t.status, t.estimate_points, t.updated_at, "
    "EXISTS (SELECT 1 FROM task_dependencies td JOIN tasks d ON d.id = td.depends_on_id "
    "        WHERE td.task_id = t.id AND d.status IN ('failed', 'cancelled', 'escalated')) "
    "  AS dependency_failed, "
    "EXISTS (SELECT 1 FROM clarification_batches cb "
    "        WHERE cb.project_id = t.project_id AND cb.status = 'open') "
    "  AS awaiting_clarification, "
    "EXISTS (SELECT 1 FROM loop_records lr "
    "        WHERE lr.task_id = t.id AND lr.status IN ('escalated', 'arbitration')) "
    "  AS loop_escalation "
    "FROM tasks t "
    "WHERE (t.status IN "
    "  ('ready', 'assigned', 'in_progress', 'in_loop', 'reopened', 'changes_requested') "
    "  OR EXISTS (SELECT 1 FROM task_blocks b "
    "             WHERE b.task_id = t.id AND b.status <> 'recovered')) "
    "  AND (CAST(:pid AS uuid) IS NULL OR t.project_id = CAST(:pid AS uuid)) "
    "ORDER BY t.project_id, t.id"
)


async def snapshots_for_sweep(
    conn: AsyncConnection, project_id: uuid.UUID | None
) -> Sequence[Row[Any]]:
    """Active tasks + their block signals (failed dep / open clarification / loop
    escalation). Scoped to one project or, with None, the whole system."""
    return (await conn.execute(_SWEEP_QUERY, {"pid": project_id})).all()


# --- milestone signal derivation (read model over the gate ledger + tasks) ---


async def stage_gate_passed(conn: AsyncConnection, project_id: uuid.UUID) -> dict[str, bool]:
    """For each pipeline stage, whether its LATEST gate evaluation passed. Reads the
    handoffs ``gate_evaluations`` ledger; a read model, never a write."""
    result = await conn.execute(
        text(
            "SELECT DISTINCT ON (stage) stage, verdict FROM gate_evaluations "
            "WHERE project_id = :pid ORDER BY stage, id DESC"
        ),
        {"pid": project_id},
    )
    return {row.stage: row.verdict == "pass" for row in result.all()}


async def open_p0_count(
    conn: AsyncConnection, project_id: uuid.UUID, *, exclude_title_prefix: str | None = None
) -> int:
    """Open critical (priority 1) tasks — the P0 proxy for milestone regression.
    ``exclude_title_prefix`` drops tasks whose title starts with it (used to omit the
    milestone machinery's own reopen defects from the count)."""
    result = await conn.execute(
        text(
            "SELECT count(*) FROM tasks WHERE project_id = :pid AND priority = 1 "
            "AND status NOT IN ('done', 'merged', 'cancelled') "
            "AND (CAST(:pfx AS text) IS NULL OR title NOT LIKE :like)"
        ),
        {
            "pid": project_id,
            "pfx": exclude_title_prefix,
            "like": (exclude_title_prefix + "%") if exclude_title_prefix else None,
        },
    )
    return int(result.scalar_one())


async def clear_assignee(conn: AsyncConnection, task_id: uuid.UUID) -> None:
    """Detach the current agent so the task can be picked up by a fresh instance —
    the reassignment rung. Bumps the optimistic-lock version."""
    await conn.execute(
        text("UPDATE tasks SET assignee_agent_id = NULL, version = version + 1 WHERE id = :tid"),
        {"tid": task_id},
    )
