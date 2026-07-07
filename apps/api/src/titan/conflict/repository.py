"""Private data access for the `conflict` module. SQLAlchemy Core over an
AsyncConnection; only ``service.py`` crosses the module boundary (TITAN-BCK-004)."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

_CONFLICT_COLS = (
    "id, project_id, conflict_id, kind, subject, parties, state, round, arbiter, "
    "opened_by, deadlock_flagged, resolution_decision_id, created_at, version"
)
_TRANSITION_COLS = (
    "id, conflict_row_id, from_state, to_state, trigger, actor, round, note, created_at"
)
_VETO_COLS = (
    "id, project_id, artifact_ref, task_id, raised_by, veto_message_id, reason, "
    "status, override_decision_id, created_at, version"
)
_THRESHOLD_COLS = "id, project_id, max_conflict_rounds, max_task_reopens, created_at, version"


# --- conflicts -------------------------------------------------------------


async def insert_conflict(
    conn: AsyncConnection,
    *,
    conflict_row_id: uuid.UUID,
    project_id: uuid.UUID,
    conflict_id: uuid.UUID,
    kind: str,
    subject: str,
    parties: str,
    state: str,
    round_no: int,
    arbiter: str | None,
    opened_by: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO conflicts (id, project_id, conflict_id, kind, subject, parties, "
            f"state, round, arbiter, opened_by) VALUES (:id, :project_id, :conflict_id, :kind, "
            f":subject, CAST(:parties AS jsonb), :state, :round, :arbiter, :opened_by) "
            f"RETURNING {_CONFLICT_COLS}"
        ),
        {
            "id": conflict_row_id,
            "project_id": project_id,
            "conflict_id": conflict_id,
            "kind": kind,
            "subject": subject,
            "parties": parties,
            "state": state,
            "round": round_no,
            "arbiter": arbiter,
            "opened_by": opened_by,
        },
    )
    return result.one()


async def get_conflict_by_cid(
    conn: AsyncConnection, project_id: uuid.UUID, conflict_id: uuid.UUID
) -> Row[Any] | None:
    result = await conn.execute(
        text(
            f"SELECT {_CONFLICT_COLS} FROM conflicts WHERE project_id = :pid AND conflict_id = :cid"
        ),
        {"pid": project_id, "cid": conflict_id},
    )
    return result.one_or_none()


async def get_conflict(conn: AsyncConnection, conflict_row_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_CONFLICT_COLS} FROM conflicts WHERE id = :id"), {"id": conflict_row_id}
    )
    return result.one_or_none()


async def update_conflict(
    conn: AsyncConnection,
    conflict_row_id: uuid.UUID,
    *,
    state: str,
    round_no: int,
    arbiter: str | None,
    deadlock_flagged: bool,
    resolution_decision_id: uuid.UUID | None,
) -> Row[Any]:
    """Set the conflict's mutable columns (the current state; the audit lives in
    the append-only ``conflict_transitions``)."""
    result = await conn.execute(
        text(
            f"UPDATE conflicts SET state = :state, round = :round, arbiter = :arbiter, "
            f"deadlock_flagged = :flagged, resolution_decision_id = :rdid "
            f"WHERE id = :id RETURNING {_CONFLICT_COLS}"
        ),
        {
            "id": conflict_row_id,
            "state": state,
            "round": round_no,
            "arbiter": arbiter,
            "flagged": deadlock_flagged,
            "rdid": resolution_decision_id,
        },
    )
    return result.one()


# --- conflict transitions (append-only) ------------------------------------


async def insert_transition(
    conn: AsyncConnection,
    *,
    transition_id: uuid.UUID,
    conflict_row_id: uuid.UUID,
    from_state: str | None,
    to_state: str,
    trigger: str,
    actor: str,
    round_no: int,
    note: str | None,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO conflict_transitions (id, conflict_row_id, from_state, to_state, "
            f"trigger, actor, round, note) VALUES (:id, :crid, :from_state, :to_state, :trigger, "
            f":actor, :round, :note) RETURNING {_TRANSITION_COLS}"
        ),
        {
            "id": transition_id,
            "crid": conflict_row_id,
            "from_state": from_state,
            "to_state": to_state,
            "trigger": trigger,
            "actor": actor,
            "round": round_no,
            "note": note,
        },
    )
    return result.one()


async def list_transitions(conn: AsyncConnection, conflict_row_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(
            f"SELECT {_TRANSITION_COLS} FROM conflict_transitions "
            f"WHERE conflict_row_id = :crid ORDER BY created_at, id"
        ),
        {"crid": conflict_row_id},
    )
    return result.all()


# --- merge vetoes ----------------------------------------------------------


async def insert_veto(
    conn: AsyncConnection,
    *,
    veto_id: uuid.UUID,
    project_id: uuid.UUID,
    artifact_ref: str,
    task_id: uuid.UUID | None,
    raised_by: str,
    veto_message_id: uuid.UUID | None,
    reason: str,
) -> Row[Any]:
    """Insert a new active veto. Raises IntegrityError if one is already active for
    the artifact (the partial-unique index) — the service treats that as idempotent."""
    result = await conn.execute(
        text(
            f"INSERT INTO merge_vetoes (id, project_id, artifact_ref, task_id, raised_by, "
            f"veto_message_id, reason) VALUES (:id, :pid, :ref, :task_id, :raised_by, :mid, "
            f":reason) RETURNING {_VETO_COLS}"
        ),
        {
            "id": veto_id,
            "pid": project_id,
            "ref": artifact_ref,
            "task_id": task_id,
            "raised_by": raised_by,
            "mid": veto_message_id,
            "reason": reason,
        },
    )
    return result.one()


async def get_active_veto_by_ref(
    conn: AsyncConnection, project_id: uuid.UUID, artifact_ref: str
) -> Row[Any] | None:
    result = await conn.execute(
        text(
            f"SELECT {_VETO_COLS} FROM merge_vetoes "
            f"WHERE project_id = :pid AND artifact_ref = :ref AND status = 'active'"
        ),
        {"pid": project_id, "ref": artifact_ref},
    )
    return result.one_or_none()


async def active_vetoes_for_task(conn: AsyncConnection, task_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(f"SELECT {_VETO_COLS} FROM merge_vetoes WHERE task_id = :tid AND status = 'active'"),
        {"tid": task_id},
    )
    return result.all()


async def mark_veto_overridden(
    conn: AsyncConnection, veto_id: uuid.UUID, *, override_decision_id: uuid.UUID
) -> Row[Any] | None:
    result = await conn.execute(
        text(
            f"UPDATE merge_vetoes SET status = 'overridden', override_decision_id = :odid "
            f"WHERE id = :id AND status = 'active' RETURNING {_VETO_COLS}"
        ),
        {"id": veto_id, "odid": override_decision_id},
    )
    return result.one_or_none()


# --- deadlock thresholds ---------------------------------------------------


async def get_thresholds_for_project(
    conn: AsyncConnection, project_id: uuid.UUID
) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_THRESHOLD_COLS} FROM conflict_thresholds WHERE project_id = :pid"),
        {"pid": project_id},
    )
    return result.one_or_none()


async def get_global_thresholds(conn: AsyncConnection) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_THRESHOLD_COLS} FROM conflict_thresholds WHERE project_id IS NULL")
    )
    return result.one_or_none()


async def upsert_thresholds(
    conn: AsyncConnection,
    *,
    threshold_id: uuid.UUID,
    project_id: uuid.UUID | None,
    max_conflict_rounds: int,
    max_task_reopens: int,
) -> Row[Any]:
    """Insert or update the (project or global) threshold row. The COALESCE unique
    index makes the NULL-project default a single upsertable row too."""
    result = await conn.execute(
        text(
            f"INSERT INTO conflict_thresholds (id, project_id, max_conflict_rounds, "
            f"max_task_reopens) VALUES (:id, :pid, :rounds, :reopens) "
            f"ON CONFLICT (COALESCE(project_id, '00000000-0000-0000-0000-000000000000'::uuid)) "
            f"DO UPDATE SET max_conflict_rounds = EXCLUDED.max_conflict_rounds, "
            f"max_task_reopens = EXCLUDED.max_task_reopens "
            f"RETURNING {_THRESHOLD_COLS}"
        ),
        {
            "id": threshold_id,
            "pid": project_id,
            "rounds": max_conflict_rounds,
            "reopens": max_task_reopens,
        },
    )
    return result.one()


async def task_exists(conn: AsyncConnection, task_id: uuid.UUID) -> bool:
    """Whether ``task_id`` is a real task — the veto-capture hook checks this before
    linking a veto to a model-supplied ``context_task_id`` so a dangling reference
    never FK-violates (which would roll back and poison-loop the whole turn)."""
    result = await conn.execute(text("SELECT 1 FROM tasks WHERE id = :tid"), {"tid": task_id})
    return result.first() is not None


async def count_task_reopens(conn: AsyncConnection, task_id: uuid.UUID) -> int:
    """How many times a task has been reopened — the durable count behind the
    rule-5 reopen deadlock. Each ``reopen`` transition writes a message whose body
    records ``action='reopen'`` (orchestrator ``_write_transition_message``), so the
    message log is the source of truth (no separate counter to drift)."""
    result = await conn.execute(
        text("SELECT count(*) FROM messages WHERE task_id = :tid AND body->>'action' = 'reopen'"),
        {"tid": task_id},
    )
    return int(result.scalar_one())
