"""Persistence for llm_usage + budget_holds (private to titan.llm).

Async SQLAlchemy Core (``text()``) like the other modules; DTOs never leak the
connection. The gateway calls these to write the per-call ledger and to read/set
the persisted budget hold.
"""

import uuid
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7


async def insert_usage(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID | None,
    task_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    agent_id: str | None,
    requested_alias: str | None,
    served_alias: str,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost_usd: Decimal,
    latency_ms: int,
    degraded_model: bool,
    price_version: int,
) -> uuid.UUID:
    row_id = uuid7()
    await conn.execute(
        text(
            "INSERT INTO llm_usage (id, project_id, task_id, run_id, agent_id, "
            "requested_alias, served_alias, provider, model, input_tokens, "
            "output_tokens, cached_tokens, cost_usd, latency_ms, degraded_model, "
            "price_version) VALUES (:id, :project_id, :task_id, :run_id, :agent_id, "
            ":requested_alias, :served_alias, :provider, :model, :input_tokens, "
            ":output_tokens, :cached_tokens, :cost_usd, :latency_ms, :degraded_model, "
            ":price_version)"
        ),
        {
            "id": row_id,
            "project_id": project_id,
            "task_id": task_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "requested_alias": requested_alias,
            "served_alias": served_alias,
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "degraded_model": degraded_model,
            "price_version": price_version,
        },
    )
    return row_id


async def get_hold(
    conn: AsyncConnection, *, project_id: uuid.UUID | None, task_id: uuid.UUID | None
) -> Row[tuple[str, str]] | None:
    """The active hold covering this project OR task, if any (scope, reason)."""
    result = await conn.execute(
        text(
            "SELECT scope, reason FROM budget_holds WHERE "
            "(scope = 'project' AND scope_id = :project_id) OR "
            "(scope = 'task' AND scope_id = :task_id) LIMIT 1"
        ),
        {"project_id": project_id, "task_id": task_id},
    )
    return result.first()


async def set_hold(
    conn: AsyncConnection, *, scope: str, scope_id: uuid.UUID, reason: str, threshold_pct: int = 100
) -> None:
    """Persist a hold; idempotent on (scope, scope_id)."""
    await conn.execute(
        text(
            "INSERT INTO budget_holds (id, scope, scope_id, reason, threshold_pct) "
            "VALUES (:id, :scope, :scope_id, :reason, :threshold_pct) "
            "ON CONFLICT (scope, scope_id) DO NOTHING"
        ),
        {
            "id": uuid7(),
            "scope": scope,
            "scope_id": scope_id,
            "reason": reason,
            "threshold_pct": threshold_pct,
        },
    )


async def clear_hold(conn: AsyncConnection, *, scope: str, scope_id: uuid.UUID) -> None:
    await conn.execute(
        text("DELETE FROM budget_holds WHERE scope = :scope AND scope_id = :scope_id"),
        {"scope": scope, "scope_id": scope_id},
    )


async def sum_cost(conn: AsyncConnection, *, scope: str, scope_id: uuid.UUID) -> Decimal:
    """Total metered cost for a project/task from the ledger (reconciliation)."""
    column = "project_id" if scope == "project" else "task_id"
    result = await conn.execute(
        text(f"SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE {column} = :id"),
        {"id": scope_id},
    )
    total = result.scalar_one()
    return Decimal(total)


__all__ = ["clear_hold", "get_hold", "insert_usage", "set_hold", "sum_cost"]
