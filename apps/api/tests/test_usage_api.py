"""GET /api/v1/usage — the LLM cost ledger's read side (Vol 4 §6.10, FRT-033).

The DoD is "the Monitoring cost-by-provider chart totals match a seeded
llm_usage fixture exactly", so these seed real rows and assert to the
micro-dollar. `cost_usd` is `Numeric(14,6)` and travels as a STRING, so exact
means exact — no float tolerance anywhere in this file.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.db import uuid7

pytestmark = pytest.mark.schema

NOW = datetime.now(tz=UTC)


def _seed(
    engine: Any,
    rows: list[dict[str, Any]],
) -> None:
    async def insert() -> None:
        async with engine.begin() as conn:
            for row in rows:
                await conn.execute(
                    text(
                        "INSERT INTO llm_usage (id, project_id, agent_id, served_alias, "
                        "provider, model, input_tokens, output_tokens, cached_tokens, "
                        "cost_usd, latency_ms, created_at) VALUES (:id, :pid, :agent, "
                        ":alias, :provider, :model, :inp, :out, :cached, :cost, 10, :at)"
                    ),
                    {
                        "id": uuid7(),
                        "pid": row.get("project_id"),
                        "agent": row.get("agent_id"),
                        "alias": row.get("alias", "chat-default"),
                        "provider": row["provider"],
                        "model": row.get("model", "m1"),
                        "inp": row.get("input_tokens", 100),
                        "out": row.get("output_tokens", 50),
                        "cached": row.get("cached_tokens", 0),
                        "cost": row["cost_usd"],
                        "at": row.get("created_at", NOW - timedelta(minutes=5)),
                    },
                )

    run_async(insert)


def _clear(engine: Any) -> None:
    async def wipe() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM llm_usage"))

    run_async(wipe)


# --- auth --------------------------------------------------------------------


def test_usage_requires_authentication(api: dict[str, Any]) -> None:
    assert api["client"].get("/api/v1/usage").status_code == 401


@pytest.mark.parametrize("role", ["owner", "admin", "maintainer", "operator", "viewer"])
def test_every_role_can_read_usage(api: dict[str, Any], role: str) -> None:
    """Deliberately NOT carved into SENSITIVE_RESOURCES, unlike logs:read.

    Aggregate spend is not a secret and not an audit row — Vol 8 §4.2 carves out
    those two — and §6.10 gates only the alert Ack at admin+, not the screen. So
    the `*:read` every bundle carries reaches this, by design.
    """
    response = api["client"].get("/api/v1/usage", headers=api["token_for"](role))
    assert response.status_code == 200


# --- the DoD -----------------------------------------------------------------


def test_cost_by_provider_totals_match_the_seeded_ledger_exactly(api: dict[str, Any]) -> None:
    """DoD: the cost-by-provider chart totals match a seeded fixture EXACTLY."""
    _clear(api["engine"])
    _seed(
        api["engine"],
        [
            {"provider": "anthropic", "cost_usd": Decimal("0.123456")},
            {"provider": "anthropic", "cost_usd": Decimal("1.000001")},
            {"provider": "openai", "cost_usd": Decimal("0.500000")},
            {"provider": "ollama", "cost_usd": Decimal("0")},
        ],
    )
    body = (
        api["client"]
        .get("/api/v1/usage?group_by=provider", headers=api["token_for"]("admin"))
        .json()
    )

    by_provider = {g["key"]: g["cost_usd"] for g in body["groups"]}
    # Exact strings, not floats: 0.123456 + 1.000001 is 1.123457 in decimal and
    # 1.1234570000000002 in binary floating point.
    assert by_provider["anthropic"] == "1.123457"
    assert by_provider["openai"] == "0.500000"
    assert by_provider["ollama"] == "0.000000"
    assert body["total"]["cost_usd"] == "1.623457"


def test_the_total_is_summed_independently_of_the_groups(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tile must not disagree with the chart it sits above.

    `total` is its own SUM over the window, never a sum of `groups`, because
    `groups` is CAPPED — deriving the total from them would silently
    under-report the moment a range holds more groups than the cap, and the tile
    would be the wrong one.

    The cap is monkeypatched down rather than seeding 101 providers, because the
    cap is the whole point: a first draft of this test seeded two groups, and
    with two groups both implementations agree, so it passed against the very
    bug it is named for. It only bites where the cap actually trims.
    """
    _clear(api["engine"])
    monkeypatch.setattr("titan.usage.service.MAX_GROUPS", 2)
    _seed(
        api["engine"],
        [
            {"provider": "a", "cost_usd": Decimal("4")},
            {"provider": "b", "cost_usd": Decimal("2")},
            {"provider": "c", "cost_usd": Decimal("1")},
        ],
    )
    body = api["client"].get("/api/v1/usage", headers=api["token_for"]("admin")).json()

    assert len(body["groups"]) == 2, "the cap should have trimmed the dearest two"
    assert body["truncated"] is True, "a trimmed chart must say so, not under-report"
    # 4 + 2 + 1, INCLUDING the group the cap dropped.
    assert body["total"]["cost_usd"] == "7.000000"
    assert body["total"]["calls"] == 3


def test_tokens_are_summed_per_group(api: dict[str, Any]) -> None:
    _clear(api["engine"])
    _seed(
        api["engine"],
        [
            {
                "provider": "anthropic",
                "cost_usd": Decimal("1"),
                "input_tokens": 10,
                "output_tokens": 5,
            },
            {
                "provider": "anthropic",
                "cost_usd": Decimal("1"),
                "input_tokens": 7,
                "output_tokens": 3,
            },
        ],
    )
    group = (
        api["client"].get("/api/v1/usage", headers=api["token_for"]("admin")).json()["groups"][0]
    )
    assert group["input_tokens"] == 17
    assert group["output_tokens"] == 8
    assert group["calls"] == 2


# --- grouping ----------------------------------------------------------------


def test_group_by_agent_returns_the_gateway_routing_key(api: dict[str, Any]) -> None:
    # `agent_id` is a routing key like `agent.ceo`, NOT an agents-table FK, so
    # it cannot resolve to a name and the UI renders it raw.
    _clear(api["engine"])
    _seed(
        api["engine"],
        [
            {"provider": "anthropic", "agent_id": "agent.ceo", "cost_usd": Decimal("2")},
            {"provider": "openai", "agent_id": "memory.ingest", "cost_usd": Decimal("1")},
        ],
    )
    body = (
        api["client"].get("/api/v1/usage?group_by=agent", headers=api["token_for"]("admin")).json()
    )
    assert {g["key"] for g in body["groups"]} == {"agent.ceo", "memory.ingest"}


def test_a_row_with_no_project_groups_under_null_rather_than_vanishing(
    api: dict[str, Any],
) -> None:
    """On a default stack this is MOST of the ledger.

    The live executor and CEO intake are both off by default, so the only
    writer is memory-ingest embeddings — which bind no project. Dropping the
    NULL group would hide the only rows a default deployment has.
    """
    _clear(api["engine"])
    _seed(api["engine"], [{"provider": "openai", "cost_usd": Decimal("1")}])
    body = (
        api["client"]
        .get("/api/v1/usage?group_by=project", headers=api["token_for"]("admin"))
        .json()
    )
    assert body["groups"] == [
        {
            "key": None,
            "cost_usd": "1.000000",
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_tokens": 0,
            "calls": 1,
        }
    ]


def test_an_unknown_group_by_is_rejected(api: dict[str, Any]) -> None:
    # The pattern is the real guard: `group_by` is interpolated into the GROUP BY,
    # a position no bind parameter can occupy.
    response = api["client"].get(
        "/api/v1/usage?group_by=provider;DROP TABLE llm_usage",
        headers=api["token_for"]("admin"),
    )
    assert response.status_code == 422


def test_groups_are_ordered_dearest_first(api: dict[str, Any]) -> None:
    _clear(api["engine"])
    _seed(
        api["engine"],
        [
            {"provider": "cheap", "cost_usd": Decimal("0.01")},
            {"provider": "dear", "cost_usd": Decimal("9.99")},
            {"provider": "middling", "cost_usd": Decimal("1.00")},
        ],
    )
    body = api["client"].get("/api/v1/usage", headers=api["token_for"]("admin")).json()
    assert [g["key"] for g in body["groups"]] == ["dear", "middling", "cheap"]


# --- the range ---------------------------------------------------------------


def test_the_range_excludes_rows_outside_it(api: dict[str, Any]) -> None:
    _clear(api["engine"])
    _seed(
        api["engine"],
        [
            {
                "provider": "recent",
                "cost_usd": Decimal("1"),
                "created_at": NOW - timedelta(minutes=5),
            },
            {
                "provider": "ancient",
                "cost_usd": Decimal("99"),
                "created_at": NOW - timedelta(days=30),
            },
        ],
    )
    body = api["client"].get("/api/v1/usage", headers=api["token_for"]("admin")).json()
    assert [g["key"] for g in body["groups"]] == ["recent"]
    assert body["total"]["cost_usd"] == "1.000000"


def test_start_after_end_is_a_422(api: dict[str, Any]) -> None:
    # `params=`, not an f-string in the URL. `NOW.isoformat()` ends in `+00:00`,
    # and a literal `+` in a query string decodes to a SPACE — so the f-string
    # version 422'd on datetime PARSING, before resolve_range's start>=end guard
    # ever ran, and would have stayed green with that guard deleted. Passing a
    # dict lets the client percent-encode the `+`, so this exercises the check
    # it is named for.
    response = api["client"].get(
        "/api/v1/usage",
        params={
            "start": NOW.isoformat(),
            "end": (NOW - timedelta(hours=1)).isoformat(),
        },
        headers=api["token_for"]("admin"),
    )
    assert response.status_code == 422
    assert "before" in response.json()["detail"]


def test_a_near_minimum_end_is_a_422_not_a_500(api: dict[str, Any]) -> None:
    # An `end` within a day of datetime.min underflows the default-window
    # subtraction with OverflowError, which is not a ValueError — so without an
    # explicit guard it escaped the router's `except ValueError` and 500'd.
    response = api["client"].get(
        "/api/v1/usage",
        params={"end": "0001-01-01T00:00:00Z"},
        headers=api["token_for"]("admin"),
    )
    assert response.status_code == 422


def test_an_explicit_range_actually_queries_that_window(api: dict[str, Any]) -> None:
    # The explicit-start/end path had ZERO passing coverage: the only test that
    # sent either param 422'd during parsing. This drives it end to end.
    _clear(api["engine"])
    _seed(
        api["engine"],
        [
            {"provider": "in", "cost_usd": Decimal("2"), "created_at": NOW - timedelta(hours=2)},
            {"provider": "out", "cost_usd": Decimal("9"), "created_at": NOW - timedelta(hours=20)},
        ],
    )
    body = (
        api["client"]
        .get(
            "/api/v1/usage",
            params={
                "start": (NOW - timedelta(hours=3)).isoformat(),
                "end": NOW.isoformat(),
            },
            headers=api["token_for"]("admin"),
        )
        .json()
    )
    assert [g["key"] for g in body["groups"]] == ["in"]
    assert body["total"]["cost_usd"] == "2.000000"


def test_an_empty_range_is_zeros_not_an_error(api: dict[str, Any]) -> None:
    # §6.10's "No data in range" state. A quiet ledger is the normal state of a
    # default stack, so this must be a clean zero rather than a 404 or a throw.
    _clear(api["engine"])
    body = api["client"].get("/api/v1/usage", headers=api["token_for"]("admin")).json()
    assert body["total"]["cost_usd"] == "0.000000"
    assert body["total"]["calls"] == 0
    assert body["groups"] == []


# --- the series --------------------------------------------------------------


def test_no_bucket_means_no_series(api: dict[str, Any]) -> None:
    _clear(api["engine"])
    _seed(api["engine"], [{"provider": "anthropic", "cost_usd": Decimal("1")}])
    body = api["client"].get("/api/v1/usage", headers=api["token_for"]("admin")).json()
    assert body["series"] == []


def test_a_bucket_returns_points_grouped_in_time(api: dict[str, Any]) -> None:
    _clear(api["engine"])
    _seed(
        api["engine"],
        [
            {
                "provider": "anthropic",
                "cost_usd": Decimal("1"),
                "created_at": NOW - timedelta(hours=2),
            },
            {
                "provider": "anthropic",
                "cost_usd": Decimal("2"),
                "created_at": NOW - timedelta(hours=2),
            },
            {
                "provider": "anthropic",
                "cost_usd": Decimal("4"),
                "created_at": NOW - timedelta(hours=1),
            },
        ],
    )
    body = api["client"].get("/api/v1/usage?bucket=hour", headers=api["token_for"]("admin")).json()
    # Two hours touched, so two points — the two same-hour rows are one point.
    assert len(body["series"]) == 2
    assert [p["cost_usd"] for p in body["series"]] == ["3.000000", "4.000000"]


def test_an_empty_bucket_produces_no_point_at_all(api: dict[str, Any]) -> None:
    # date_trunc, not generate_series: a 7-day minute-bucketed range would
    # otherwise be ~10k rows of zeros. The client draws the gaps.
    _clear(api["engine"])
    _seed(
        api["engine"],
        [
            {"provider": "a", "cost_usd": Decimal("1"), "created_at": NOW - timedelta(hours=5)},
            {"provider": "a", "cost_usd": Decimal("1"), "created_at": NOW - timedelta(hours=1)},
        ],
    )
    body = api["client"].get("/api/v1/usage?bucket=hour", headers=api["token_for"]("admin")).json()
    assert len(body["series"]) == 2  # not 5


# --- project scope -----------------------------------------------------------


def test_a_project_filter_narrows_to_that_project(api: dict[str, Any]) -> None:
    _clear(api["engine"])
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    _seed(
        api["engine"],
        [
            {"provider": "a", "cost_usd": Decimal("1"), "project_id": mine},
            {"provider": "a", "cost_usd": Decimal("50"), "project_id": theirs},
            {"provider": "a", "cost_usd": Decimal("7")},  # no project
        ],
    )
    body = (
        api["client"]
        .get(f"/api/v1/usage?project_id={mine}", headers=api["token_for"]("admin"))
        .json()
    )
    assert body["total"]["cost_usd"] == "1.000000"
