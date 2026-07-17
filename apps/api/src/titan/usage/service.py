"""Aggregate the LLM cost ledger for Monitoring (Vol 4 §6.10, TITAN-FRT-033)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.usage import repository as repo

GroupBy = Literal["provider", "model", "agent", "project", "alias"]
Bucket = Literal["minute", "hour", "day"]

#: The default window when the caller gives no range. The bucket-per-range
#: mapping (§6.10's picker) lives on the FRONTEND, which owns the picker and
#: sends an explicit `bucket`; a duplicate table here was dead and drifted.
DEFAULT_RANGE_S = 24 * 3600

#: Guards a pathological range from returning a pathological result set. A 7-day
#: hourly series grouped by model is 168 buckets x N models; this is the ceiling
#: on the rows, not on the range.
MAX_ROWS = 5_000
MAX_GROUPS = 100


class UsageTotals(BaseModel):
    """Money is a STRING on the wire.

    `cost_usd` is `Numeric(14,6)` and Pydantic renders a Decimal as a string
    precisely so it does not go through a float on the way out — a JSON number
    would round the sixth decimal, and these totals are asserted to the cent
    against a seeded fixture.
    """

    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    calls: int


class UsageGroup(UsageTotals):
    #: The group's value — `anthropic`, `agent.ceo`, a project uuid as text.
    #: Null where the column is (e.g. a gateway call with no project bound),
    #: which is most of them on a default stack.
    key: str | None


class UsagePoint(BaseModel):
    bucket_at: datetime
    key: str | None
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    calls: int


class UsageSummary(BaseModel):
    """What Monitoring reads: the tiles, the group-by chart, and the series."""

    start: datetime
    end: datetime
    group_by: str
    total: UsageTotals
    groups: list[UsageGroup]
    #: Empty unless a `bucket` was asked for. An empty bucket produces no point
    #: at all — the client draws the gap.
    series: list[UsagePoint] = []
    #: True when the row cap trimmed the answer, so the UI can say the chart is
    #: partial instead of quietly under-reporting a total.
    truncated: bool = False


def resolve_range(
    start: datetime | None, end: datetime | None, now: datetime
) -> tuple[datetime, datetime]:
    resolved_end = end or now
    try:
        resolved_start = start or resolved_end - timedelta(seconds=DEFAULT_RANGE_S)
    except OverflowError as exc:
        # `end` within a day of datetime.min underflows the default-window
        # subtraction. OverflowError is NOT a ValueError, so without this it
        # escapes the router's `except ValueError` and 500s a client typo that
        # should be a 422.
        raise ValueError("end is too far in the past") from exc
    if resolved_start.tzinfo is None:
        resolved_start = resolved_start.replace(tzinfo=UTC)
    if resolved_end.tzinfo is None:
        resolved_end = resolved_end.replace(tzinfo=UTC)
    if resolved_start >= resolved_end:
        raise ValueError("start must be before end")
    return resolved_start, resolved_end


async def summarize(
    conn: AsyncConnection,
    *,
    start: datetime,
    end: datetime,
    group_by: GroupBy,
    project_id: uuid.UUID | None = None,
    bucket: Bucket | None = None,
) -> UsageSummary:
    """Totals, per-group totals and (optionally) the time series.

    `total` is computed by its own SUM over the same window rather than by
    adding up `groups`. That is deliberate: `groups` is capped at MAX_GROUPS, so
    summing it would silently under-report the total the moment a range has more
    than 100 models in it — the tile would disagree with the chart it sits above,
    and the tile would be the one that is wrong.
    """
    total_row = await repo.totals(conn, start=start, end=end, project_id=project_id)
    group_rows = await repo.grouped(
        conn,
        start=start,
        end=end,
        project_id=project_id,
        group_by=group_by,
        limit=MAX_GROUPS + 1,
    )
    truncated = len(group_rows) > MAX_GROUPS
    group_rows = group_rows[:MAX_GROUPS]

    points: list[UsagePoint] = []
    if bucket is not None:
        point_rows = await repo.series(
            conn,
            start=start,
            end=end,
            project_id=project_id,
            group_by=group_by,
            bucket=bucket,
            limit=MAX_ROWS + 1,
        )
        series_truncated = len(point_rows) > MAX_ROWS
        truncated = truncated or series_truncated
        points = [
            UsagePoint(
                bucket_at=row.bucket_at,
                key=_text(row.group_key),
                cost_usd=row.cost_usd,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                calls=row.calls,
            )
            for row in point_rows[:MAX_ROWS]
        ]
        # The repository returns buckets NEWEST-first (so the cap keeps the
        # newest); the chart reads oldest-to-now, so sort back to ascending.
        points.sort(key=lambda p: p.bucket_at)

    total = (
        UsageTotals(
            cost_usd=total_row.cost_usd,
            input_tokens=total_row.input_tokens,
            output_tokens=total_row.output_tokens,
            cached_tokens=total_row.cached_tokens,
            calls=total_row.calls,
        )
        if total_row is not None
        else UsageTotals(
            cost_usd=Decimal(0), input_tokens=0, output_tokens=0, cached_tokens=0, calls=0
        )
    )

    return UsageSummary(
        start=start,
        end=end,
        group_by=group_by,
        total=total,
        groups=[
            UsageGroup(
                key=_text(row.group_key),
                cost_usd=row.cost_usd,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cached_tokens=row.cached_tokens,
                calls=row.calls,
            )
            for row in group_rows
        ],
        series=points,
        truncated=truncated,
    )


def _text(value: object) -> str | None:
    return None if value is None else str(value)
