"""Data access for the ``evolution_artifacts`` ledger (TITAN-EVO-002).

Connection-scoped functions (the caller owns the transaction), mirroring the
other repositories. Only immutable references are stored — never prompt text.
"""

import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import Table, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.db import uuid7
from titan.evolution.models import EvolutionArtifact

_T = cast(Table, EvolutionArtifact.__table__)


async def record_artifact(
    conn: AsyncConnection,
    *,
    kind: str,
    ref: str,
    path: str,
    content_sha256: str,
    commit_sha: str | None,
    semver: str,
    ep: str,
    approved_by: uuid.UUID | None,
    approved_at: datetime | None,
    diff_url: str = "",
) -> None:
    """Insert (or, for the same bytes, refresh the approval of) one artifact row.

    Idempotent on ``(path, content_sha256)``: syncing the manifest then approving
    the same bytes updates the approval signature in place rather than duplicating
    the immutable reference."""
    stmt = insert(_T).values(
        kind=kind,
        ref=ref,
        path=path,
        content_sha256=content_sha256,
        commit_sha=commit_sha,
        semver=semver,
        ep=ep,
        approved_by=approved_by,
        approved_at=approved_at,
        diff_url=diff_url,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_evolution_artifacts_path_sha",
        set_={
            "commit_sha": stmt.excluded.commit_sha,
            "semver": stmt.excluded.semver,
            "ep": stmt.excluded.ep,
            "approved_by": stmt.excluded.approved_by,
            "approved_at": stmt.excluded.approved_at,
            "diff_url": stmt.excluded.diff_url,
        },
    )
    await conn.execute(stmt)


def _row_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "ref": row["ref"],
        "path": row["path"],
        "content_sha256": row["content_sha256"],
        "commit_sha": row["commit_sha"],
        "semver": row["semver"],
        "ep": row["ep"],
        "approved_by": str(row["approved_by"]) if row["approved_by"] else None,
        "approved_at": row["approved_at"].isoformat() if row["approved_at"] else None,
        "diff_url": row["diff_url"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


async def list_artifacts(
    conn: AsyncConnection,
    *,
    kind: str | None = None,
    ref: str | None = None,
    approved_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    stmt = select(_T)
    if kind is not None:
        stmt = stmt.where(_T.c.kind == kind)
    if ref is not None:
        stmt = stmt.where(_T.c.ref == ref)
    if approved_only:
        stmt = stmt.where(_T.c.approved_by.is_not(None))
    stmt = stmt.order_by(_T.c.created_at.desc()).limit(limit)
    result = await conn.execute(stmt)
    return [_row_dict(r._mapping) for r in result.all()]


async def get_artifact(
    conn: AsyncConnection, *, path: str, content_sha256: str
) -> dict[str, Any] | None:
    stmt = select(_T).where(_T.c.path == path, _T.c.content_sha256 == content_sha256)
    row = (await conn.execute(stmt)).first()
    return _row_dict(row._mapping) if row is not None else None


# --- scorecards (Phase 84, TITAN-EVO-005) -------------------------------------


async def scorecard_exists(engine: AsyncEngine, task_id: uuid.UUID) -> bool:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT 1 FROM agent_scorecards WHERE task_id = :tid LIMIT 1"), {"tid": task_id}
        )
        return row.first() is not None


async def scorecard_task_meta(engine: AsyncEngine, task_id: uuid.UUID) -> dict[str, Any] | None:
    """Objective telemetry for a task: executor role, terminal status, and the
    metering/attempt/wall-clock rollup from llm_usage + runs (TITAN-EVO-005)."""
    sql = text(
        "SELECT t.project_id, t.status, t.title, t.spec, a.role AS assignee_role, "
        "COALESCE((SELECT SUM(cost_usd) FROM llm_usage WHERE task_id=t.id),0) AS cost_usd, "
        "COALESCE((SELECT SUM(input_tokens) FROM llm_usage WHERE task_id=t.id),0) AS tokens_in, "
        "COALESCE((SELECT SUM(output_tokens) FROM llm_usage WHERE task_id=t.id),0) AS tokens_out, "
        "COALESCE((SELECT MAX(attempt) FROM runs WHERE task_id=t.id),1) AS max_attempt, "
        "COALESCE((SELECT EXTRACT(EPOCH FROM (MAX(finished_at)-MIN(started_at))) FROM runs "
        "  WHERE task_id=t.id AND started_at IS NOT NULL AND finished_at IS NOT NULL),0) "
        "  AS wall_s, "
        "(SELECT model FROM runs WHERE task_id=t.id AND model IS NOT NULL "
        "  ORDER BY created_at DESC LIMIT 1) AS model_id "
        "FROM tasks t LEFT JOIN agents a ON a.id = t.assignee_agent_id WHERE t.id = :tid"
    )
    async with engine.connect() as conn:
        row = (await conn.execute(sql, {"tid": task_id})).mappings().first()
    if row is None:
        return None
    return {
        "project_id": row["project_id"],
        "status": row["status"],
        "title": row["title"],
        "spec": row["spec"],
        "assignee_role": row["assignee_role"],
        "cost_usd": Decimal(row["cost_usd"]),
        "tokens_in": int(row["tokens_in"]),
        "tokens_out": int(row["tokens_out"]),
        "max_attempt": int(row["max_attempt"]),
        "wall_clock_s": int(round(float(row["wall_s"]))),
        "model_id": row["model_id"],
    }


async def insert_scorecard(
    engine: AsyncEngine,
    *,
    task_id: uuid.UUID,
    agent_id: str,
    agent_spec_sha: str,
    prompt_sha: str,
    model_id: str,
    task_class: str,
    dimensions: dict[str, int],
    composite_score: Decimal,
    cost_usd: Decimal,
    tokens_in: int,
    tokens_out: int,
    wall_clock_s: int,
    trace_id: str,
) -> dict[str, Any] | None:
    """Insert one scorecard; returns the row, or None if a scorecard for this task
    already exists (the uq_agent_scorecards_task index makes a race a no-op)."""
    row_id = uuid7()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO agent_scorecards (id, task_id, agent_id, agent_spec_sha, prompt_sha, "
                "model_id, task_class, dimensions, composite_score, cost_usd, tokens_in, "
                "tokens_out, wall_clock_s, trace_id) VALUES "
                "(:id,:task_id,:agent_id,:spec,:prompt,:model,:cls,"
                "CAST(:dims AS JSONB),:comp,:cost,:ti,:to,:wall,:trace) "
                "ON CONFLICT (task_id) DO NOTHING"
            ),
            {
                "id": row_id,
                "task_id": task_id,
                "agent_id": agent_id,
                "spec": agent_spec_sha,
                "prompt": prompt_sha,
                "model": model_id,
                "cls": task_class,
                "dims": json.dumps(dimensions),
                "comp": composite_score,
                "cost": cost_usd,
                "ti": tokens_in,
                "to": tokens_out,
                "wall": wall_clock_s,
                "trace": trace_id,
            },
        )
        if result.rowcount == 0:
            return None  # a scorecard for this task already exists (race lost)
    return {
        "id": str(row_id),
        "task_id": str(task_id),
        "agent_id": agent_id,
        "prompt_sha": prompt_sha,
        "agent_spec_sha": agent_spec_sha,
        "model_id": model_id,
        "task_class": task_class,
        "dimensions": dimensions,
        "composite_score": float(composite_score),
        "trace_id": trace_id,
    }


async def scorecards_since(
    engine: AsyncEngine,
    *,
    days: int,
    agent_id: str | None = None,
    before_days: int | None = None,
) -> list[dict[str, Any]]:
    """Scorecards in the trailing ``days`` window. ``before_days`` sets an UPPER
    bound (created_at < now()-before_days), so a baseline window can EXCLUDE the
    recent window it is compared against (a diluted baseline masks real drops)."""
    clause = "" if agent_id is None else " AND agent_id = :agent"
    upper = (
        "" if before_days is None else " AND created_at < now() - make_interval(days => :before)"
    )
    sql = text(
        "SELECT agent_id, task_class, dimensions, composite_score, cost_usd, tokens_in, "
        "tokens_out, wall_clock_s, created_at FROM agent_scorecards "
        "WHERE created_at >= now() - make_interval(days => :days)" + clause + upper
    )
    params: dict[str, Any] = {"days": days}
    if agent_id is not None:
        params["agent"] = agent_id
    if before_days is not None:
        params["before"] = before_days
    async with engine.connect() as conn:
        rows = (await conn.execute(sql, params)).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        dims = r["dimensions"]
        out.append(
            {
                "agent_id": r["agent_id"],
                "task_class": r["task_class"],
                "dimensions": json.loads(dims) if isinstance(dims, str) else dims,
                "composite_score": float(r["composite_score"]),
                "cost_usd": float(r["cost_usd"]),
                "tokens_in": int(r["tokens_in"]),
                "tokens_out": int(r["tokens_out"]),
                "wall_clock_s": int(r["wall_clock_s"]),
            }
        )
    return out


# --- investigations (Phase 84, TITAN-EVO-007) ---------------------------------


async def open_investigation(
    engine: AsyncEngine,
    *,
    agent_id: str,
    task_class: str,
    window_days: int,
    recent_value: float,
    baseline_value: float,
    drop_pct: float,
    series_snapshot: dict[str, Any],
    kpi: str = "composite",
    assignee_role: str = "memory",
) -> str | None:
    """Open an investigation for a sustained KPI drop. Idempotent: the partial
    unique index (one open per agent+class) means a repeat detection is a no-op.
    Returns the new row id, or None if an open one already exists."""
    row_id = uuid7()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO evolution_investigations (id, agent_id, task_class, kpi, window_days, "
                "recent_value, baseline_value, drop_pct, series_snapshot, assignee_role) "
                "VALUES (:id,:agent,:cls,:kpi,:win,:recent,:base,:drop,CAST(:snap AS JSONB),:role) "
                "ON CONFLICT (agent_id, task_class) WHERE status = 'open' DO NOTHING"
            ),
            {
                "id": row_id,
                "agent": agent_id,
                "cls": task_class,
                "kpi": kpi,
                "win": window_days,
                "recent": recent_value,
                "base": baseline_value,
                "drop": drop_pct,
                "snap": json.dumps(series_snapshot),
                "role": assignee_role,
            },
        )
        return str(row_id) if result.rowcount == 1 else None


async def list_investigations(
    engine: AsyncEngine, *, status: str = "open", limit: int = 100
) -> list[dict[str, Any]]:
    sql = text(
        "SELECT id, agent_id, task_class, kpi, window_days, recent_value, baseline_value, "
        "drop_pct, series_snapshot, assignee_role, status, created_at "
        "FROM evolution_investigations "
        "WHERE status = :status ORDER BY created_at DESC LIMIT :limit"
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(sql, {"status": status, "limit": limit})).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        snap = r["series_snapshot"]
        out.append(
            {
                "id": str(r["id"]),
                "agent_id": r["agent_id"],
                "task_class": r["task_class"],
                "kpi": r["kpi"],
                "window_days": int(r["window_days"]),
                "recent_value": float(r["recent_value"]),
                "baseline_value": float(r["baseline_value"]),
                "drop_pct": float(r["drop_pct"]),
                "series_snapshot": json.loads(snap) if isinstance(snap, str) else snap,
                "assignee_role": r["assignee_role"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )
    return out


__all__ = [
    "get_artifact",
    "insert_scorecard",
    "list_artifacts",
    "list_investigations",
    "open_investigation",
    "record_artifact",
    "scorecard_exists",
    "scorecard_task_meta",
    "scorecards_since",
]
