"""SQL over ``llm_usage``. Private to :mod:`titan.usage`."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

#: The dimensions Monitoring can group cost by, mapped to the column that backs
#: each one. An allowlist, not a format string: the value is interpolated into
#: the GROUP BY, which is a position no bind parameter can occupy.
GROUP_COLUMNS: dict[str, str] = {
    "provider": "provider",
    "model": "model",
    # A routing key like `agent.ceo` (llm/tables.py:29) — NOT an agents FK, so
    # it cannot be joined to a name and the UI must render the raw key.
    "agent": "agent_id",
    "project": "project_id::text",
    # The alias actually served, which differs from the requested one whenever
    # the gateway fell back to a cheaper/available tier.
    "alias": "served_alias",
}


async def totals(
    conn: AsyncConnection,
    *,
    start: datetime,
    end: datetime,
    project_id: uuid.UUID | None,
) -> Row[Any] | None:
    sql = """
        SELECT COALESCE(SUM(cost_usd), 0)::numeric(14, 6) AS cost_usd,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COUNT(*) AS calls
        FROM llm_usage
        WHERE created_at >= :start AND created_at < :end
    """
    params: dict[str, Any] = {"start": start, "end": end}
    if project_id is not None:
        sql += " AND project_id = :project_id"
        params["project_id"] = project_id
    result = await conn.execute(text(sql), params)
    return result.first()


async def grouped(
    conn: AsyncConnection,
    *,
    start: datetime,
    end: datetime,
    project_id: uuid.UUID | None,
    group_by: str,
    limit: int,
) -> list[Row[Any]]:
    """Cost and tokens per group key, dearest first.

    ``group_by`` is looked up in GROUP_COLUMNS by the caller, so only the five
    known column expressions ever reach the statement — the key itself never
    does.
    """
    column = GROUP_COLUMNS[group_by]
    sql = f"""
        SELECT {column} AS group_key,
               COALESCE(SUM(cost_usd), 0)::numeric(14, 6) AS cost_usd,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COUNT(*) AS calls
        FROM llm_usage
        WHERE created_at >= :start AND created_at < :end
    """
    params: dict[str, Any] = {"start": start, "end": end, "limit": limit}
    if project_id is not None:
        sql += " AND project_id = :project_id"
        params["project_id"] = project_id
    # Cost DESC then the key, so a page of ties is stable rather than shuffling
    # between identical requests.
    sql += f" GROUP BY {column} ORDER BY cost_usd DESC, group_key NULLS LAST LIMIT :limit"
    result = await conn.execute(text(sql), params)
    return list(result.all())


async def series(
    conn: AsyncConnection,
    *,
    start: datetime,
    end: datetime,
    project_id: uuid.UUID | None,
    group_by: str,
    bucket: str,
    limit: int,
) -> list[Row[Any]]:
    """The same aggregate, bucketed over time — the chart's points.

    ``date_trunc`` rather than a generated series: an empty bucket produces no
    row at all, and the client fills the gaps. Emitting zero-rows for every
    quiet minute of a 7-day range would be ~10k rows of nothing.
    """
    column = GROUP_COLUMNS[group_by]
    sql = f"""
        SELECT date_trunc(:bucket, created_at) AS bucket_at,
               {column} AS group_key,
               COALESCE(SUM(cost_usd), 0)::numeric(14, 6) AS cost_usd,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COUNT(*) AS calls
        FROM llm_usage
        WHERE created_at >= :start AND created_at < :end
    """
    params: dict[str, Any] = {
        "start": start,
        "end": end,
        "bucket": bucket,
        "limit": limit,
    }
    if project_id is not None:
        sql += " AND project_id = :project_id"
        params["project_id"] = project_id
    # DESC, so that if the LIMIT trims the result it keeps the NEWEST buckets —
    # a chart is read right-to-"now", so dropping the oldest tail is far less
    # misleading than ORDER BY ASC would be (which silently ends the chart
    # before now). The service sorts back to ascending for display.
    sql += f" GROUP BY bucket_at, {column} ORDER BY bucket_at DESC LIMIT :limit"
    result = await conn.execute(text(sql), params)
    return list(result.all())
