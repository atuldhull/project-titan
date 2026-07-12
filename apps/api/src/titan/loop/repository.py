"""Private data access for the self-improvement loop. SQLAlchemy Core over an
AsyncConnection; only ``service`` is importable across the module boundary."""

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7

_LOOP_COLS = (
    "id, task_id, project_id, status, iteration, cycle_count, max_cycles, termination_reason, "
    "created_at, updated_at, version"
)
_FINDING_COLS = (
    "id, loop_id, task_id, raised_cycle, severity, file, line, rationale, suggestion, "
    "fingerprint, hunk_key, disposition, refutation_reason, refute_upheld_count, conflict_id, "
    "created_at, updated_at, version"
)
_CYCLE_COLS = "id, loop_id, cycle_number, new_findings_count, clean, outcome, created_at"
_EVENT_COLS = "id, loop_id, task_id, cycle_number, stage, actor, verdict, detail, at"


# --- loop record ------------------------------------------------------------


async def latest_loop(conn: AsyncConnection, task_id: uuid.UUID) -> Row[Any] | None:
    """The task's most recent loop iteration (a reopened task runs fresh iterations;
    the merge gate + reads always consult the latest)."""
    result = await conn.execute(
        text(
            f"SELECT {_LOOP_COLS} FROM loop_records WHERE task_id = :tid "
            "ORDER BY iteration DESC LIMIT 1"
        ),
        {"tid": task_id},
    )
    return result.one_or_none()


async def create_loop_record(
    conn: AsyncConnection,
    task_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    iteration: int,
    max_cycles: int,
) -> Row[Any]:
    """Open a fresh loop iteration for the task. The UNIQUE(task_id, iteration)
    constraint serializes concurrent starters onto one iteration."""
    result = await conn.execute(
        text(
            "INSERT INTO loop_records (id, task_id, project_id, iteration, max_cycles) "
            "VALUES (:id, :tid, :pid, :it, :mc) "
            f"RETURNING {_LOOP_COLS}"
        ),
        {"id": uuid7(), "tid": task_id, "pid": project_id, "it": iteration, "mc": max_cycles},
    )
    return result.one()


async def supersede_converged_loop(conn: AsyncConnection, task_id: uuid.UUID) -> None:
    """On task reopen (done → reopened): a converged loop from the prior iteration no
    longer covers the re-worked code, so mark it ``superseded`` — the merge gate then
    blocks until a fresh loop converges (TITAN-WFL-010 across iterations)."""
    await conn.execute(
        text(
            "UPDATE loop_records SET status = 'superseded', version = version + 1 "
            "WHERE task_id = :tid AND status = 'converged'"
        ),
        {"tid": task_id},
    )


async def trailing_clean_count(conn: AsyncConnection, loop_id: uuid.UUID) -> int:
    """The run of clean cycles at the tail of the loop — the convergence streak to
    restore when resuming a partially-run loop after a crash (TITAN-WFL-013)."""
    rows = await conn.execute(
        text("SELECT clean FROM loop_cycles WHERE loop_id = :l ORDER BY cycle_number DESC"),
        {"l": loop_id},
    )
    streak = 0
    for row in rows:
        if not row.clean:
            break
        streak += 1
    return streak


async def set_loop_status(
    conn: AsyncConnection,
    loop_id: uuid.UUID,
    *,
    status: str,
    termination_reason: str | None = None,
    cycle_count: int | None = None,
) -> Row[Any]:
    sets = ["status = :status", "version = version + 1"]
    params: dict[str, Any] = {"id": loop_id, "status": status}
    if termination_reason is not None:
        sets.append("termination_reason = :tr")
        params["tr"] = termination_reason
    if cycle_count is not None:
        sets.append("cycle_count = :cc")
        params["cc"] = cycle_count
    result = await conn.execute(
        text(f"UPDATE loop_records SET {', '.join(sets)} WHERE id = :id RETURNING {_LOOP_COLS}"),
        params,
    )
    return result.one()


async def set_cycle_count(conn: AsyncConnection, loop_id: uuid.UUID, count: int) -> None:
    await conn.execute(
        text("UPDATE loop_records SET cycle_count = :cc, version = version + 1 WHERE id = :id"),
        {"id": loop_id, "cc": count},
    )


# --- cycles (append-only) ---------------------------------------------------


async def insert_cycle(
    conn: AsyncConnection,
    *,
    loop_id: uuid.UUID,
    cycle_number: int,
    new_findings_count: int,
    clean: bool,
    outcome: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            "INSERT INTO loop_cycles "
            "(id, loop_id, cycle_number, new_findings_count, clean, outcome) "
            "VALUES (:id, :lid, :n, :nf, :clean, :outcome) "
            f"RETURNING {_CYCLE_COLS}"
        ),
        {
            "id": uuid7(),
            "lid": loop_id,
            "n": cycle_number,
            "nf": new_findings_count,
            "clean": clean,
            "outcome": outcome,
        },
    )
    return result.one()


async def cycles_for_loop(conn: AsyncConnection, loop_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(f"SELECT {_CYCLE_COLS} FROM loop_cycles WHERE loop_id = :l ORDER BY cycle_number ASC"),
        {"l": loop_id},
    )
    return result.all()


# --- findings ---------------------------------------------------------------


async def insert_finding_if_new(
    conn: AsyncConnection,
    *,
    loop_id: uuid.UUID,
    task_id: uuid.UUID,
    raised_cycle: int,
    severity: str,
    file: str,
    line: int,
    rationale: str,
    suggestion: str,
    fingerprint: str,
    hunk_key: str,
) -> bool:
    """Insert a finding; a no-op if this fingerprint already exists in the loop
    (a re-asserted finding, not a new one). Returns True iff it was new."""
    result = await conn.execute(
        text(
            "INSERT INTO loop_findings (id, loop_id, task_id, raised_cycle, severity, file, line, "
            "rationale, suggestion, fingerprint, hunk_key) "
            "VALUES (:id, :lid, :tid, :rc, :sev, :file, :line, :rat, :sug, :fp, :hk) "
            "ON CONFLICT (loop_id, fingerprint) DO NOTHING RETURNING id"
        ),
        {
            "id": uuid7(),
            "lid": loop_id,
            "tid": task_id,
            "rc": raised_cycle,
            "sev": severity,
            "file": file,
            "line": line,
            "rat": rationale,
            "sug": suggestion,
            "fp": fingerprint,
            "hk": hunk_key,
        },
    )
    return result.one_or_none() is not None


async def set_finding_disposition(
    conn: AsyncConnection,
    *,
    loop_id: uuid.UUID,
    fingerprint: str,
    disposition: str,
    refutation_reason: str | None = None,
) -> None:
    await conn.execute(
        text(
            "UPDATE loop_findings SET disposition = :d, refutation_reason = :r, "
            "version = version + 1 WHERE loop_id = :l AND fingerprint = :fp"
        ),
        {"l": loop_id, "fp": fingerprint, "d": disposition, "r": refutation_reason},
    )


async def bump_refute_upheld(
    conn: AsyncConnection, *, loop_id: uuid.UUID, fingerprint: str, reason: str | None
) -> int:
    """Increment the refuted-and-upheld counter and mark the finding ``upheld``;
    return the new count (2 routes to arbitration, TITAN-WFL-012)."""
    result = await conn.execute(
        text(
            "UPDATE loop_findings SET refute_upheld_count = refute_upheld_count + 1, "
            "disposition = 'upheld', refutation_reason = :r, version = version + 1 "
            "WHERE loop_id = :l AND fingerprint = :fp RETURNING refute_upheld_count"
        ),
        {"l": loop_id, "fp": fingerprint, "r": reason},
    )
    return int(result.scalar_one())


async def set_finding_arbitration(
    conn: AsyncConnection, *, loop_id: uuid.UUID, fingerprint: str, conflict_id: uuid.UUID
) -> None:
    await conn.execute(
        text(
            "UPDATE loop_findings SET disposition = 'arbitration', conflict_id = :cid, "
            "version = version + 1 WHERE loop_id = :l AND fingerprint = :fp"
        ),
        {"l": loop_id, "fp": fingerprint, "cid": conflict_id},
    )


async def open_minor_plus_count(conn: AsyncConnection, loop_id: uuid.UUID) -> int:
    """Count findings still unresolved (open or upheld) at severity ≥ minor — the
    backlog a clean cycle must have emptied. "≥ minor" matches the engine's
    ``is_minor_plus``: everything EXCEPT ``info`` (an unknown/out-of-enum severity
    is treated as ≥ minor, never silently dropped — closing the fail-open where an
    upheld 'critical' finding would slip past a literal severity allow-list)."""
    result = await conn.execute(
        text(
            "SELECT count(*) FROM loop_findings WHERE loop_id = :l "
            "AND disposition IN ('open', 'upheld') AND severity <> 'info'"
        ),
        {"l": loop_id},
    )
    return int(result.scalar_one())


async def findings_for_loop(conn: AsyncConnection, loop_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(
            f"SELECT {_FINDING_COLS} FROM loop_findings WHERE loop_id = :l "
            "ORDER BY raised_cycle ASC, created_at ASC"
        ),
        {"l": loop_id},
    )
    return result.all()


# --- stage events (append-only) ---------------------------------------------


async def insert_stage_event(
    conn: AsyncConnection,
    *,
    loop_id: uuid.UUID,
    task_id: uuid.UUID,
    cycle_number: int,
    stage: str,
    actor: str,
    verdict: str,
    detail: dict[str, Any],
) -> None:
    await conn.execute(
        text(
            "INSERT INTO loop_stage_events "
            "(loop_id, task_id, cycle_number, stage, actor, verdict, detail) "
            "VALUES (:lid, :tid, :n, :stage, :actor, :verdict, CAST(:detail AS jsonb))"
        ),
        {
            "lid": loop_id,
            "tid": task_id,
            "n": cycle_number,
            "stage": stage,
            "actor": actor,
            "verdict": verdict,
            "detail": json.dumps(detail),
        },
    )


async def stage_events_for_loop(conn: AsyncConnection, loop_id: uuid.UUID) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(f"SELECT {_EVENT_COLS} FROM loop_stage_events WHERE loop_id = :l ORDER BY id ASC"),
        {"l": loop_id},
    )
    return result.all()


__all__ = [
    "bump_refute_upheld",
    "create_loop_record",
    "cycles_for_loop",
    "findings_for_loop",
    "insert_cycle",
    "insert_finding_if_new",
    "insert_stage_event",
    "latest_loop",
    "open_minor_plus_count",
    "set_cycle_count",
    "set_finding_arbitration",
    "set_finding_disposition",
    "set_loop_status",
    "stage_events_for_loop",
    "supersede_converged_loop",
    "trailing_clean_count",
]
