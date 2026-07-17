"""Log paging: the cursor, and the duplicates it exists to prevent.

Loki has no cursor. Its `query_range` takes `start`/`end`/`direction`/`limit`
and nothing else, so paging means walking one bound of the window and re-asking
— and because `start` is INCLUSIVE and `end` is EXCLUSIVE, the boundary
nanosecond sits in BOTH windows. Every line sharing it comes back a second
time. That overlap is what the cursor's `seen` set removes, and it is the whole
reason this file exists.

Pure: no app, no HTTP, no database, so these run without a Loki AND without the
`schema` marker's PostgreSQL. `run_async` drives the coroutines — this repo has
no pytest-asyncio (see conftest).
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from tests.conftest import run_async
from titan.gateway.pagination import decode_cursor
from titan.logs import service
from titan.logs.logql import LogFilters
from titan.logs.loki import LokiEntry
from titan.logs.service import query_logs, resolve_window

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 7, 16, 11, 0, tzinfo=UTC)
FILTERS = LogFilters()

#: Derived from the window rather than hardcoded. A literal here silently sat
#: OUTSIDE [WINDOW_START, NOW) and ScriptedLoki filtered every fixture line
#: away — the tests failed loudly, but a laxer fake would have passed them all
#: while proving nothing.
BASE = int(service.to_ns(datetime(2026, 7, 16, 11, 30, tzinfo=UTC)))


class ScriptedLoki:
    """Returns whatever entries fall inside the window it is asked for.

    Models the four things about Loki that make paging hard, and the fourth is
    the one that matters most:

    1. `start` is inclusive and `end` is exclusive.
    2. `limit` is a hard ceiling, applied GLOBALLY.
    3. `direction` picks which end of the window it takes them from.
    4. **The response is GROUPED BY STREAM, not globally sorted.** Loki nests
       entries under their label set, so a response spanning several streams
       comes back as sorted RUNS in stream order — a globally interleaved order
       is not even representable in that JSON. The first version of this fake
       did `window.sort(...)` and handed back one flat sorted list, which no
       real multi-stream response has ever been. Every paging test passed, and
       they were all vacuous: they never exercised the only shape production
       actually sees, and a real bug (pages both repeating and silently
       dropping lines) sat behind them.

    `label_of` splits entries into streams the way promtail really does — one
    stream per (service, level) — so `{namespace="titan"}`, the query built
    when no filter is set, spans several.
    """

    def __init__(self, entries: list[LokiEntry]) -> None:
        self.entries = entries

    async def query_range(
        self, query: str, *, start_ns: str, end_ns: str, limit: int, direction: str
    ) -> list[LokiEntry]:
        window = [e for e in self.entries if int(start_ns) <= int(e.ts_ns) < int(end_ns)]
        # Loki applies the limit globally, at the extreme end named by direction.
        window.sort(key=lambda e: int(e.ts_ns), reverse=direction == "backward")
        selected = window[:limit]

        # …and only THEN groups what survived into per-stream buckets. The
        # caller receives the concatenation of those buckets, in label order.
        buckets: dict[str, list[LokiEntry]] = {}
        for entry in selected:
            buckets.setdefault(_label_key(entry), []).append(entry)
        return [entry for key in sorted(buckets) for entry in buckets[key]]


def _label_key(entry: LokiEntry) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(entry.labels.items()))


def _entry(ts_ns: int, text: str = "x", *, level: str = "info") -> LokiEntry:
    """One entry. `level` decides which STREAM it lands in — promtail promotes
    it to a label, so alternating it is what makes a response multi-stream."""
    return LokiEntry(
        ts_ns=str(ts_ns),
        line=text,
        labels={"namespace": "titan", "service": "api", "level": level},
    )


def _interleaved(count: int, *, start: int = 0) -> list[LokiEntry]:
    """Lines alternating between two streams — the ordinary case.

    `{namespace="titan"}` (the query with no filters) spans one stream per
    (service, level) pair, so a plain run of application logs is already
    multi-stream. Nothing here is pathological.
    """
    return [
        _entry(BASE + i, f"line-{i}", level="error" if i % 2 else "info")
        for i in range(start, start + count)
    ]


def _window(**over: Any) -> service.LogWindow:
    kwargs: dict[str, Any] = {
        "start": WINDOW_START,
        "end": NOW,
        "direction": "backward",
        "limit": 2,
        "position": None,
        "now": NOW,
    }
    kwargs.update(over)
    return resolve_window(**kwargs)


def _page(loki: ScriptedLoki, **over: Any) -> Any:
    return run_async(lambda: query_logs(loki, filters=FILTERS, window=_window(**over)))


def _drain(loki: ScriptedLoki, *, limit: int, direction: str = "backward") -> list[str]:
    """Page all the way through and return every line id seen, in order."""
    ids: list[str] = []
    cursor: str | None = None
    for _ in range(50):  # a scan needing >50 pages here is a bug, not a fixture
        page = _page(
            loki,
            limit=limit,
            direction=direction,
            position=decode_cursor(cursor) if cursor else None,
        )
        ids.extend(item.id for item in page.items)
        if page.next_cursor is None:
            return ids
        cursor = page.next_cursor
    raise AssertionError("paging did not terminate")


def test_paging_backward_returns_every_line_exactly_once() -> None:
    loki = ScriptedLoki([_entry(BASE + i, f"line-{i}") for i in range(7)])
    ids = _drain(loki, limit=2)
    assert len(ids) == 7
    assert len(set(ids)) == 7, "a line came back on two pages"


def test_paging_forward_returns_every_line_exactly_once() -> None:
    loki = ScriptedLoki([_entry(BASE + i, f"line-{i}") for i in range(7)])
    ids = _drain(loki, limit=2, direction="forward")
    assert len(ids) == 7
    assert len(set(ids)) == 7


def test_a_multi_stream_response_pages_without_repeating_or_dropping() -> None:
    """The case a globally-sorted fake can never see.

    Loki groups its response by stream, so flattening it gives sorted RUNS, not
    a sorted list. Truncating that to `limit` discards an arbitrary entry
    instead of the sentinel, and reading `page[-1].ts_ns` as the boundary takes
    the extreme timestamp off the wrong entry — which both returns lines twice
    AND steps the window past lines no page ever returns.

    Eight lines alternating info/error is not a contrived fixture: it is what
    `{namespace="titan"}` looks like whenever an app logs two levels.
    """
    loki = ScriptedLoki(_interleaved(8))
    ids = _drain(loki, limit=3)

    assert len(set(ids)) == len(ids), "a line was returned on two pages"
    assert len(ids) == 8, f"lines were silently skipped: got {len(ids)} of 8"


def test_a_multi_stream_response_pages_forward_intact_too() -> None:
    loki = ScriptedLoki(_interleaved(8))
    ids = _drain(loki, limit=3, direction="forward")
    assert len(set(ids)) == len(ids)
    assert len(ids) == 8


def test_a_multi_stream_page_is_ordered_by_time_not_by_stream() -> None:
    """A page must read as a log, not as one stream after another."""
    loki = ScriptedLoki(_interleaved(6))
    page = _page(loki, limit=6)
    stamps = [int(item.ts_ns) for item in page.items]
    assert stamps == sorted(stamps, reverse=True), f"page is not in time order: {stamps}"


def test_lines_sharing_the_boundary_nanosecond_are_not_repeated() -> None:
    """The case the cursor's `seen` set exists for.

    Three lines at one nanosecond with a page size of 2: the boundary lands
    mid-cohort, so the next window necessarily re-includes lines already shown.
    """
    loki = ScriptedLoki(
        [
            _entry(BASE + 10, "newest"),
            _entry(BASE, "same-ns-a"),
            _entry(BASE, "same-ns-b"),
            _entry(BASE, "same-ns-c"),
        ]
    )
    ids = _drain(loki, limit=2)
    assert len(ids) == 4
    assert len(set(ids)) == 4


def test_a_full_page_of_one_nanosecond_still_makes_progress() -> None:
    """The pathological case: more lines at one nanosecond than a page holds.

    Every line the follow-up query returns was already shown, so the page
    de-duplicates to empty. Answering `next_cursor=None` there would end the
    scan in the middle of the logs; instead the boundary steps off the
    nanosecond. That skips its remaining lines — the bounded loss
    MAX_BOUNDARY_IDS documents — but the scan terminates and reaches what is
    beyond it.
    """
    loki = ScriptedLoki(
        [_entry(BASE, f"crowd-{i}") for i in range(5)] + [_entry(BASE - 100, "older")]
    )
    ids = _drain(loki, limit=2)  # must terminate rather than loop forever
    assert len(ids) == len(set(ids)), "progress was made by repeating lines"
    assert service.line_id(str(BASE - 100), "older") in ids, "the scan never got past the crowd"


def test_the_last_page_has_no_cursor() -> None:
    loki = ScriptedLoki([_entry(BASE + i) for i in range(2)])
    assert _page(loki, limit=5).next_cursor is None


def test_a_full_page_of_distinct_lines_offers_a_cursor() -> None:
    loki = ScriptedLoki([_entry(BASE + i, f"l{i}") for i in range(5)])
    page = _page(loki, limit=2)
    assert page.next_cursor is not None
    assert len(page.items) == 2


def test_next_page_is_not_cut_short_by_the_dedupe() -> None:
    """`has_more` must come from Loki's count, not the de-duplicated one.

    Loki returns limit+1 as the has-more sentinel. If the boundary cohort trims
    the list back to `limit` and has_more were decided from THAT, the scan would
    stop early — a silent truncation that reads as "no more logs".
    """
    loki = ScriptedLoki([_entry(BASE + i, f"l{i}") for i in range(10)])
    first = _page(loki, limit=3)
    assert first.next_cursor is not None
    second = _page(loki, limit=3, position=decode_cursor(first.next_cursor))
    assert second.next_cursor is not None
    assert not {i.id for i in first.items} & {i.id for i in second.items}


# --- resolve_window ----------------------------------------------------------


def test_the_cursors_direction_wins_over_the_query_parameter() -> None:
    # Mirrors keyset_from: a client flipping direction mid-scan would otherwise
    # seek from a position that means the opposite thing.
    window = _window(direction="backward", position={"ts_ns": str(BASE), "dir": "forward"})
    assert window.direction == "forward"


def test_a_backward_cursor_keeps_its_boundary_nanosecond_in_the_window() -> None:
    # `end` is EXCLUSIVE, so resuming at exactly the boundary would drop every
    # line sharing it. +1 keeps them and `seen` removes the duplicates.
    window = _window(position={"ts_ns": str(BASE), "dir": "backward"})
    assert window.end_ns == str(BASE + 1)


def test_a_forward_cursor_resumes_at_its_boundary_nanosecond() -> None:
    # `start` is INCLUSIVE, so the boundary is naturally re-fetched.
    window = _window(position={"ts_ns": str(BASE), "dir": "forward"})
    assert window.start_ns == str(BASE)


def test_the_window_is_not_baked_into_the_cursor() -> None:
    """A tail polls forward with `end` freshly set to now on every request.

    Freezing `end` in the cursor would stop the tail dead after one page: it
    would keep re-querying the same closed window forever.
    """
    later = datetime(2026, 7, 16, 13, 0, tzinfo=UTC)
    window = _window(end=later, position={"ts_ns": str(BASE), "dir": "forward"})
    assert window.end_ns == service.to_ns(later)


def test_a_cursor_without_a_position_is_rejected() -> None:
    with pytest.raises(ValueError, match="position"):
        _window(position={"dir": "backward"})


def test_a_non_numeric_boundary_is_rejected() -> None:
    # The cursor is opaque but not trusted — it round-trips through the client.
    with pytest.raises(ValueError, match="position"):
        _window(position={"ts_ns": "'; DROP TABLE", "dir": "backward"})


def test_an_unknown_direction_in_a_cursor_is_rejected() -> None:
    with pytest.raises(ValueError, match="direction"):
        _window(position={"ts_ns": str(BASE), "dir": "sideways"})


def test_start_after_end_is_rejected() -> None:
    with pytest.raises(ValueError, match="before"):
        _window(start=NOW, end=WINDOW_START)


def test_a_naive_datetime_is_read_as_utc() -> None:
    assert service.to_ns(datetime(2026, 7, 16, 12, 0)) == service.to_ns(NOW)


def test_to_ns_is_exact_rather_than_rounded_through_a_float() -> None:
    """`int(moment.timestamp() * 1e9)` rounds by hundreds of nanoseconds.

    1.78e18 ns is far past a float's 2**53 of integer precision, so the
    multiply lands up to ~256ns either side of the truth. Either side is the
    problem: a bound the caller widened on purpose gets silently narrowed back,
    dropping the boundary lines the widening existed to keep.
    """
    for micros in (1, 999_999, 123_456, 111_111):
        moment = datetime(2026, 7, 16, 12, 0, 0, micros, tzinfo=UTC)
        exact = service.to_ns(moment)
        assert exact.endswith("000"), "a datetime is microsecond-resolution"
        # The exact answer, computed a different way.
        expected = (int(moment.timestamp() // 1) * 1_000_000 + micros) * 1_000
        assert int(exact) == expected, f"{micros}us drifted by {int(exact) - expected}ns"


def test_the_cursor_does_not_grow_while_the_boundary_advances() -> None:
    """`seen` may only accumulate while the boundary is genuinely stuck.

    At limit=1 every page is exactly one nanosecond wide, so a "did this page
    span more than one ns" test never fires and `seen` grows by one id per page
    forever — a cursor that is a URL parameter reaching 13KB while paging
    perfectly ordinary, distinctly-timestamped lines.
    """
    loki = ScriptedLoki([_entry(BASE + i, f"line-{i}") for i in range(40)])
    cursor: str | None = None
    widest = 0
    for _ in range(40):
        page = _page(loki, limit=1, position=decode_cursor(cursor) if cursor else None)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        widest = max(widest, len(cursor))
    # Every line has its own nanosecond, so the cohort is always a single id.
    assert widest < 200, f"the cursor grew to {widest} chars while the boundary was moving"


def test_line_ids_are_stable_and_content_addressed() -> None:
    assert service.line_id("1", "a") == service.line_id("1", "a")
    assert service.line_id("1", "a") != service.line_id("1", "b")
    assert service.line_id("1", "a") != service.line_id("2", "a")


def test_every_typed_field_is_subtracted_from_the_expansion() -> None:
    """PROMOTED_KEYS and LogLine's typed fields must not drift apart.

    `fields` is Vol 9's `attrs` reconstructed by subtraction, so a key promoted
    to a typed field but left out of PROMOTED_KEYS gets reported twice — once
    typed, once again inside the line-expansion panel. Only a structural check
    catches that; a comment asking the next person to remember will not.
    """
    from titan.logs.logql import PROMOTED_KEYS
    from titan.logs.service import LogLine

    # Not parsed out of the JSON body: Loki supplies these, or they ARE the
    # expansion.
    not_from_body = {"id", "ts_ns", "line", "labels", "fields"}
    typed = set(LogLine.model_fields) - not_from_body
    assert typed <= PROMOTED_KEYS, f"typed but still in `fields`: {sorted(typed - PROMOTED_KEYS)}"
