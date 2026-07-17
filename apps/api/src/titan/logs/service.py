"""Query log history and shape it for the viewer (Vol 4 §6.7).

The public surface of :mod:`titan.logs`. Everything here is about turning
Loki's answer — a flat list of ``(nanosecond, raw line)`` pairs — into
structured, page-able log lines with a cursor that does not repeat itself.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from titan.gateway.pagination import Page, encode_cursor
from titan.logs import logql
from titan.logs.loki import MAX_ENTRIES, LokiEntry, LokiQuerier

#: Loki's default window when neither bound is given.
DEFAULT_WINDOW_S = 3600

#: How many ids the cursor may carry to de-duplicate a page boundary. The
#: boundary cohort is normally one or two lines; this only binds in the
#: pathological case where a whole page shares one nanosecond, and it is what
#: stops the cursor from growing without limit. See `_next_cursor`.
MAX_BOUNDARY_IDS = 256

DIRECTIONS = ("backward", "forward")


class LogLine(BaseModel):
    """One structured log line.

    ``ts_ns`` is a **string**, not a number: it is a Unix nanosecond timestamp
    (> 2**53), so JSON's number type would round it and the id, the cursor and
    the sort order would all drift. Clients divide it down for display.
    """

    #: Stable across pages: the same line always gets the same id, so a client
    #: splicing a gap-fill into its buffer can de-duplicate on it.
    id: str
    ts_ns: str
    #: The raw line exactly as Loki stored it — what "copy line" copies, and
    #: the only thing present for a container that does not log JSON.
    line: str
    #: The Loki stream labels: namespace, service (the COMPOSE service name),
    #: and level. `service` here is `api`; the body's `service` is `titan-api`.
    labels: dict[str, str]
    level: str | None = None
    event: str | None = None
    module: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    #: The caller's own kwargs — Vol 9 §6.3's `attrs`, reconstructed by
    #: subtraction (structlog flattens them to the top level). Drives §6.7's
    #: "line expansion for structured JSON payloads".
    fields: dict[str, Any] = {}


@dataclass(frozen=True)
class LogWindow:
    """The resolved time range and paging position for one query."""

    start_ns: str
    end_ns: str
    direction: str
    limit: int
    seen_ids: frozenset[str] = frozenset()
    #: The nanosecond the incoming cursor resumed from, if any. Needed to make
    #: progress when a whole page is de-duplicated away (see `_next_cursor`).
    boundary_ns: str | None = None


def line_id(ts_ns: str, line: str) -> str:
    """A stable id for a log line.

    Loki has no line identity, so we mint one from the two things that define
    the entry. Two byte-identical lines at the same nanosecond collide — but
    they are genuinely indistinguishable in Loki too, so no id scheme could
    separate them.
    """
    digest = hashlib.sha256(line.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{ts_ns}:{digest}"


#: Unix epoch, for exact integer arithmetic against a datetime.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_ns(moment: datetime) -> str:
    """RFC 3339 -> Unix nanoseconds, exactly.

    Integer arithmetic, NOT `int(moment.timestamp() * 1e9)`. `timestamp()`
    returns a float, and ~1.78e18 nanoseconds is far past a float's 2**53 of
    integer precision, so that multiply rounds by a couple of hundred
    nanoseconds *in either direction* — silently NARROWING a window the caller
    deliberately widened, which drops the very boundary lines the widening
    existed to keep.

    A datetime is still microsecond-resolution, so the last three digits are
    zero. That is a real limit of the wire format and it is why the CURSOR
    carries Loki's `ts_ns` verbatim instead of round-tripping it through a
    datetime — but it is now a clean truncation rather than a rounding error.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    delta = moment - _EPOCH
    micros = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    return str(micros * 1_000)


def parse_line(entry: LokiEntry) -> LogLine:
    """Structure one Loki entry. Never raises: a non-JSON line is normal.

    postgres, redis and grafana all ship plain text through the same promtail,
    so roughly half of what is in Loki is not a structlog object at all.
    """
    body: dict[str, Any] = {}
    try:
        parsed = json.loads(entry.line)
        if isinstance(parsed, dict):
            body = parsed
    except (json.JSONDecodeError, ValueError):
        body = {}

    def text(key: str) -> str | None:
        value = body.get(key)
        return value if isinstance(value, str) else None

    return LogLine(
        id=line_id(entry.ts_ns, entry.line),
        ts_ns=entry.ts_ns,
        line=entry.line,
        labels=entry.labels,
        # The label is promtail's promoted copy and the body is structlog's
        # original. They agree when both exist; the label wins because it is
        # the one the stream was selected by, and it is the only one a
        # plain-text container can have.
        level=entry.labels.get("level") or text("level"),
        event=text("event"),
        module=text("module"),
        request_id=text("request_id"),
        trace_id=text("trace_id"),
        span_id=text("span_id"),
        project_id=text("project_id"),
        task_id=text("task_id"),
        agent_id=text("agent_id"),
        fields=logql.split_body(body),
    )


def resolve_window(
    *,
    start: datetime | None,
    end: datetime | None,
    direction: str,
    limit: int,
    position: dict[str, Any] | None,
    now: datetime,
) -> LogWindow:
    """Fold the request's time range together with a cursor position.

    The cursor's ``direction`` wins over the query parameter, mirroring
    ``keyset_from``: a client that flips direction mid-scan would otherwise
    seek from a position that means the opposite thing.

    Only the *position* is baked into the cursor, never the whole window — a
    tail polls forward with ``end`` freshly set to now on every request, so
    freezing ``end`` would stop the tail dead after one page.
    """
    resolved_end = end or now
    resolved_start = start or datetime.fromtimestamp(
        resolved_end.timestamp() - DEFAULT_WINDOW_S, tz=UTC
    )
    if resolved_start >= resolved_end:
        raise ValueError("start must be before end")

    start_ns = to_ns(resolved_start)
    end_ns = to_ns(resolved_end)
    seen: frozenset[str] = frozenset()
    boundary: str | None = None

    if position is not None:
        cursor_direction = position.get("dir")
        if isinstance(cursor_direction, str):
            direction = cursor_direction
        if direction not in DIRECTIONS:
            raise ValueError(f"unknown direction {direction!r}")
        boundary = _position_str(position, "ts_ns")
        if boundary is None or not boundary.isdigit():
            raise ValueError("cursor is missing a valid position")
        raw_seen = position.get("seen")
        if isinstance(raw_seen, list):
            seen = frozenset(str(s) for s in raw_seen)

        if direction == "backward":
            # Walk `end` down. Loki's `end` is EXCLUSIVE, so +1 keeps the
            # boundary nanosecond in the window; `seen` then removes the lines
            # from it we already returned.
            end_ns = _min_ns(end_ns, str(int(boundary) + 1))
        else:
            # Walk `start` up. `start` is INCLUSIVE, so the boundary itself is
            # re-fetched and `seen` removes the duplicates.
            start_ns = _max_ns(start_ns, boundary)

    return LogWindow(
        start_ns=start_ns,
        end_ns=end_ns,
        direction=direction,
        limit=limit,
        seen_ids=seen,
        boundary_ns=boundary,
    )


def _position_str(position: dict[str, Any], key: str) -> str | None:
    value = position.get(key)
    return value if isinstance(value, str) else None


def _min_ns(a: str, b: str) -> str:
    return a if int(a) <= int(b) else b


def _max_ns(a: str, b: str) -> str:
    return a if int(a) >= int(b) else b


async def query_logs(
    querier: LokiQuerier,
    *,
    filters: logql.LogFilters,
    window: LogWindow,
) -> Page[LogLine]:
    """Run one page of the log query.

    Asks Loki for one entry more than the page size — the same sentinel trick
    the SQL keyset uses — so "is there a next page" is answered without a
    second round trip.
    """
    query = logql.build_query(filters)
    entries = await querier.query_range(
        query,
        start_ns=window.start_ns,
        end_ns=window.end_ns,
        # One more than the page, as the has-more sentinel. Clamped because
        # Loki REJECTS a limit above its own `max_entries_limit` rather than
        # clamping it — without this, asking for the endpoint's own published
        # maximum (5000) would send 5001 and 400 every time.
        limit=min(window.limit + 1, MAX_ENTRIES),
        direction=window.direction,
    )

    # THE thing Loki's response shape gets wrong if you trust it: `data.result`
    # is an ARRAY OF STREAMS, each internally sorted, and `limit` is applied
    # GLOBALLY across them. Flattening therefore yields a sequence of sorted
    # RUNS, not a sorted list — and the default query `{namespace="titan"}`
    # spans one stream per (service, level) pair, so this is the normal case,
    # not an edge case. Without this sort, `lines[:limit]` truncates an
    # arbitrary entry rather than the sentinel, and `page[-1].ts_ns` reads a
    # boundary off the wrong entry — which both repeats lines across pages and
    # steps the window past lines that are never returned by any page.
    entries = sorted(
        entries,
        key=lambda e: int(e.ts_ns),
        reverse=window.direction == "backward",
    )

    # "Is there another page" is Loki's answer, NOT the de-duplicated one: the
    # sentinel says Loki hit the ceiling we asked for. Deciding this after the
    # dedupe would end the scan early whenever the boundary cohort happened to
    # trim the list back to `limit` — a silent truncation that looks like the
    # end of the logs.
    has_more = len(entries) > window.limit

    lines = [parse_line(e) for e in entries]
    # The boundary cohort from the previous page. Loki has no idea we already
    # returned these, so it hands them back every time the window re-includes
    # their nanosecond; dropping them here is what makes paging duplicate-free.
    if window.seen_ids:
        lines = [line for line in lines if line.id not in window.seen_ids]

    page = lines[: window.limit]
    cursor = _next_cursor(page, window) if has_more else None
    return Page(items=page, next_cursor=cursor)


def _next_cursor(page: list[LogLine], window: LogWindow) -> str | None:
    """The position to resume from: the last line's nanosecond plus its cohort.

    ``seen`` needs only the ids sharing the boundary nanosecond, because the
    next window re-includes exactly that nanosecond and nothing else we have
    already shown. It has to *accumulate* across pages only in the pathological
    case where consecutive pages end on the same nanosecond; `MAX_BOUNDARY_IDS`
    bounds that, and when it is hit we let the boundary advance rather than
    grow the cursor forever — which can skip lines at that one nanosecond, but
    a cursor that cannot advance would hang the viewer instead.
    """
    if not page:
        # Every line Loki returned was one we had already shown, yet it still
        # hit the ceiling: a whole page's worth of lines share one nanosecond.
        # Stepping off that nanosecond is the only way to make progress —
        # returning None here would end the scan in the middle of the logs.
        if window.boundary_ns is None:
            return None
        return encode_cursor(
            {"ts_ns": _advance(window.boundary_ns, window.direction), "dir": window.direction}
        )

    boundary = page[-1].ts_ns
    cohort = {line.id for line in page if line.ts_ns == boundary}
    # Carry the previous cohort forward ONLY while the boundary is genuinely
    # stuck on the same nanosecond. Testing "did this page span >1 nanosecond"
    # instead looks equivalent and is not: at limit=1 a page is ALWAYS exactly
    # one nanosecond, so that test never fires, `seen` accumulates one id per
    # page forever, and the cursor — a URL query parameter — grows past 13KB
    # (and past a proxy's header cap) while paging perfectly ordinary lines.
    stuck = window.boundary_ns == boundary
    carried = cohort | window.seen_ids if stuck else cohort
    if len(carried) > MAX_BOUNDARY_IDS:
        return encode_cursor(
            {"ts_ns": _advance(boundary, window.direction), "dir": window.direction}
        )
    return encode_cursor({"ts_ns": boundary, "dir": window.direction, "seen": sorted(carried)})


def _advance(ts_ns: str, direction: str) -> str:
    """Step the boundary off a nanosecond we can no longer de-duplicate.

    Backward walks `end` down and `end` is exclusive, so -1 puts the whole
    nanosecond behind the window. Forward walks `start` up and `start` is
    inclusive, so +1 does the same. Either way the lines still sitting at that
    nanosecond are skipped — the bounded loss `MAX_BOUNDARY_IDS` documents.
    """
    return str(int(ts_ns) - 1) if direction == "backward" else str(int(ts_ns) + 1)
