"""GET /api/v1/logs — the Loki-backed log history (Vol 4 §6.7, Phase 62).

Loki is Compose-profile-gated, so CI has none. That is exactly why
:class:`titan.logs.loki.LokiQuerier` is a Protocol: the fake below records the
LogQL it was handed, which lets these tests assert on the QUERY we build — the
part that carries all the version-specific rules — and not merely on a
response we also wrote.
"""

import json
from typing import Any

import pytest

from titan.logs.loki import LokiEntry

pytestmark = pytest.mark.schema


class FakeLoki:
    """Records every query and replays a scripted list of entries."""

    def __init__(self, entries: list[LokiEntry] | None = None) -> None:
        self.entries = entries if entries is not None else []
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def query_range(
        self, query: str, *, start_ns: str, end_ns: str, limit: int, direction: str
    ) -> list[LokiEntry]:
        self.calls.append(
            {
                "query": query,
                "start_ns": start_ns,
                "end_ns": end_ns,
                "limit": limit,
                "direction": direction,
            }
        )
        if self.error is not None:
            raise self.error
        return self.entries[:limit]

    @property
    def query(self) -> str:
        return str(self.calls[-1]["query"])


def _line(ts_ns: str, **body: Any) -> LokiEntry:
    payload = {"ts": "2026-07-16T12:00:00Z", "level": "info", "event": "hello", **body}
    return LokiEntry(
        ts_ns=ts_ns,
        line=json.dumps(payload),
        labels={"namespace": "titan", "service": "api", "level": str(payload["level"])},
    )


def _install(api: dict[str, Any], loki: FakeLoki) -> None:
    api["client"].app.state.loki = loki


# --- auth --------------------------------------------------------------------


def test_logs_require_authentication(api: dict[str, Any]) -> None:
    _install(api, FakeLoki())
    assert api["client"].get("/api/v1/logs").status_code == 401


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_owner_and_admin_can_read_logs(api: dict[str, Any], role: str) -> None:
    _install(api, FakeLoki([_line("1700000000000000000")]))
    response = api["client"].get("/api/v1/logs", headers=api["token_for"](role))
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


@pytest.mark.parametrize("role", ["maintainer", "operator", "viewer"])
def test_the_read_wildcard_does_not_reach_the_logs(api: dict[str, Any], role: str) -> None:
    """The security property of the whole endpoint.

    maintainer/operator/viewer all hold `*:read`. `logs` is in
    SENSITIVE_RESOURCES precisely so that does not grant `logs:read` — and
    `operator` is the bundle every AGENT runs as, so this is also what stops an
    agent reading the system logs back. Org-scoped route, so 403 (there is no
    project whose existence a 404 would be hiding).
    """
    _install(api, FakeLoki([_line("1700000000000000000")]))
    response = api["client"].get("/api/v1/logs", headers=api["token_for"](role))
    assert response.status_code == 403


# --- Loki absent -------------------------------------------------------------


def test_no_loki_configured_is_a_503_that_names_the_setting(api: dict[str, Any]) -> None:
    # The default Compose stack has no Loki (it is behind --profile obs), so
    # this is the normal state, not a failure — but it must not look like
    # "there are no logs".
    api["client"].app.state.loki = None
    response = api["client"].get("/api/v1/logs", headers=api["token_for"]("admin"))
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "TITAN_LOKI_URL" in response.json()["detail"]


def test_loki_unreachable_is_a_503_not_a_500(api: dict[str, Any]) -> None:
    from titan.logs.loki import LokiUnavailable

    loki = FakeLoki()
    loki.error = LokiUnavailable("connection refused")
    _install(api, loki)
    response = api["client"].get("/api/v1/logs", headers=api["token_for"]("admin"))
    assert response.status_code == 503


def test_loki_rejecting_the_query_is_a_422_not_a_500(api: dict[str, Any]) -> None:
    from titan.logs.loki import LokiQueryError

    loki = FakeLoki()
    loki.error = LokiQueryError("parse error at line 1, col 3")
    _install(api, loki)
    response = api["client"].get("/api/v1/logs", headers=api["token_for"]("admin"))
    assert response.status_code == 422
    assert "parse error" in response.json()["detail"]


# --- the query we build ------------------------------------------------------


def test_query_always_pins_namespace(api: dict[str, Any]) -> None:
    loki = FakeLoki()
    _install(api, loki)
    api["client"].get("/api/v1/logs", headers=api["token_for"]("admin"))
    assert loki.query.startswith('{namespace="titan"}')


def test_service_and_level_filters_become_label_matchers(api: dict[str, Any]) -> None:
    loki = FakeLoki()
    _install(api, loki)
    api["client"].get(
        "/api/v1/logs?service=api&level=error",
        headers=api["token_for"]("admin"),
    )
    assert loki.query.startswith('{namespace="titan", service="api", level="error"}')


def test_repeated_filters_become_an_anchored_alternation(api: dict[str, Any]) -> None:
    # Label matchers are FULLY anchored, so no .* is needed — and must not be
    # added, or `service=~"api|worker"` would stop matching exactly.
    loki = FakeLoki()
    _install(api, loki)
    api["client"].get(
        "/api/v1/logs?service=api&service=worker&level=error&level=warning",
        headers=api["token_for"]("admin"),
    )
    assert 'service=~"api|worker"' in loki.query
    assert 'level=~"error|warning"' in loki.query


def test_an_unknown_level_is_a_422_naming_it(api: dict[str, Any]) -> None:
    # structlog only ever writes debug|info|warning|error|critical, lowercase.
    # "WARN" matches nothing in Loki, so failing loudly beats a silent 0 rows.
    _install(api, FakeLoki())
    response = api["client"].get("/api/v1/logs?level=WARN", headers=api["token_for"]("admin"))
    assert response.status_code == 422
    assert "WARN" in response.json()["detail"]


def test_search_is_a_line_filter_and_precedes_json(api: dict[str, Any]) -> None:
    loki = FakeLoki()
    _install(api, loki)
    api["client"].get(
        "/api/v1/logs?search=timeout&project_id=p1",
        headers=api["token_for"]("admin"),
    )
    query = loki.query
    # The cheap substring scan must run before the expensive parse, or `| json`
    # parses every line in the window.
    assert query.index('|= "timeout"') < query.index("| json")


def test_body_filters_name_their_fields_to_dodge_the_extracted_rename(
    api: dict[str, Any],
) -> None:
    """`| json` on this deployment renames `level`/`service` to `*_extracted`.

    promtail promotes both to stream labels and structlog also writes both into
    the body, so a bare `| json | level="x"` filters on a field Loki has
    renamed. Naming the outputs ourselves sidesteps the whole rule.
    """
    loki = FakeLoki()
    _install(api, loki)
    api["client"].get(
        "/api/v1/logs?project_id=abc&task_id=def",
        headers=api["token_for"]("admin"),
    )
    query = loki.query
    assert '| json f_project_id="project_id", f_task_id="task_id"' in query
    assert '| f_project_id = "abc"' in query
    assert '| f_task_id = "def"' in query


def test_no_body_filters_means_no_json_stage(api: dict[str, Any]) -> None:
    # `| json` is the expensive stage. Nothing to filter on the body means it
    # should not run at all — the endpoint still parses the line itself.
    loki = FakeLoki()
    _install(api, loki)
    api["client"].get("/api/v1/logs?service=api", headers=api["token_for"]("admin"))
    assert "| json" not in loki.query


def test_a_quote_in_a_filter_cannot_escape_the_string(api: dict[str, Any]) -> None:
    loki = FakeLoki()
    _install(api, loki)
    api["client"].get(
        '/api/v1/logs?search=say "hi"\\ and more',
        headers=api["token_for"]("admin"),
    )
    # Escaped, not raw: a bare " would close the LogQL string early and let the
    # rest of the value be parsed as query syntax.
    assert '|= "say \\"hi\\"\\\\ and more"' in loki.query


def test_regex_using_lookahead_is_rejected_with_a_reason(api: dict[str, Any]) -> None:
    # Loki's RE2 has no lookaround. Python's `re` compiles it happily, so
    # without an explicit check this would be a confusing upstream 400.
    _install(api, FakeLoki())
    response = api["client"].get("/api/v1/logs?regex=foo(?!bar)", headers=api["token_for"]("admin"))
    assert response.status_code == 422
    assert "lookahead" in response.json()["detail"]


def test_an_invalid_regex_is_a_422(api: dict[str, Any]) -> None:
    _install(api, FakeLoki())
    response = api["client"].get("/api/v1/logs?regex=foo(", headers=api["token_for"]("admin"))
    assert response.status_code == 422


# --- limits + direction ------------------------------------------------------


def test_limit_above_lokis_own_ceiling_is_rejected_by_us(api: dict[str, Any]) -> None:
    # Loki REJECTS limit > max_entries_limit (5000) with a 400 rather than
    # clamping, so validating here turns a 400 from a foreign service into a
    # normal 422 on our own parameter.
    _install(api, FakeLoki())
    response = api["client"].get("/api/v1/logs?limit=5000", headers=api["token_for"]("admin"))
    assert response.status_code == 422


def test_the_published_maximum_limit_actually_works(api: dict[str, Any]) -> None:
    """The ceiling we advertise must be one a caller can actually use.

    The query asks Loki for `limit + 1` as its has-more sentinel, so publishing
    Loki's own 5000 would send 5001 and 400 on every request — the documented
    maximum would be the one value guaranteed to fail. MAX_PAGE is one below.
    """
    from titan.logs.loki import MAX_ENTRIES, MAX_PAGE

    loki = FakeLoki()
    _install(api, loki)
    response = api["client"].get(
        f"/api/v1/logs?limit={MAX_PAGE}", headers=api["token_for"]("admin")
    )
    assert response.status_code == 200
    assert loki.calls[-1]["limit"] <= MAX_ENTRIES


def test_a_sentinel_entry_is_requested_so_next_page_needs_no_second_trip(
    api: dict[str, Any],
) -> None:
    loki = FakeLoki()
    _install(api, loki)
    api["client"].get("/api/v1/logs?limit=10", headers=api["token_for"]("admin"))
    assert loki.calls[-1]["limit"] == 11


def test_direction_defaults_to_backward(api: dict[str, Any]) -> None:
    loki = FakeLoki()
    _install(api, loki)
    api["client"].get("/api/v1/logs", headers=api["token_for"]("admin"))
    assert loki.calls[-1]["direction"] == "backward"


def test_an_unknown_direction_is_rejected(api: dict[str, Any]) -> None:
    _install(api, FakeLoki())
    response = api["client"].get(
        "/api/v1/logs?direction=sideways", headers=api["token_for"]("admin")
    )
    assert response.status_code == 422


# --- structuring the line ----------------------------------------------------


def test_a_structlog_line_is_parsed_into_fields(api: dict[str, Any]) -> None:
    loki = FakeLoki(
        [
            _line(
                "1700000000000000000",
                level="error",
                event="boom",
                module="titan.runs.service",
                request_id="req-1",
                project_id="proj-1",
                attempt=3,
            )
        ]
    )
    _install(api, loki)
    item = api["client"].get("/api/v1/logs", headers=api["token_for"]("admin")).json()["items"][0]
    assert item["event"] == "boom"
    assert item["module"] == "titan.runs.service"
    assert item["request_id"] == "req-1"
    assert item["project_id"] == "proj-1"
    # Vol 9 §6.3's `attrs` does not exist — structlog flattens kwargs to the top
    # level — so `fields` is the body MINUS the pipeline's own keys.
    assert item["fields"] == {"attempt": 3}
    assert "level" not in item["fields"]


def test_a_plain_text_line_survives_without_json(api: dict[str, Any]) -> None:
    # postgres/redis/grafana all ship plain text through the same promtail, so
    # roughly half of what is in Loki is not a structlog object.
    loki = FakeLoki(
        [
            LokiEntry(
                ts_ns="1700000000000000000",
                line="LOG:  database system is ready to accept connections",
                labels={"namespace": "titan", "service": "postgres"},
            )
        ]
    )
    _install(api, loki)
    item = api["client"].get("/api/v1/logs", headers=api["token_for"]("admin")).json()["items"][0]
    assert item["line"].startswith("LOG:")
    assert item["event"] is None
    assert item["level"] is None  # no level label either: the json stage found none
    assert item["fields"] == {}


def test_ts_ns_stays_a_string(api: dict[str, Any]) -> None:
    # A Unix nanosecond exceeds 2**53. As a JSON number it would round, and the
    # id, the cursor and the sort order would all drift with it.
    loki = FakeLoki([_line("1700000000123456789")])
    _install(api, loki)
    body = api["client"].get("/api/v1/logs", headers=api["token_for"]("admin")).text
    assert '"ts_ns":"1700000000123456789"' in body.replace(", ", ",").replace('": "', '":"')


def test_the_label_level_wins_over_the_body(api: dict[str, Any]) -> None:
    entry = LokiEntry(
        ts_ns="1700000000000000000",
        line=json.dumps({"level": "info", "event": "x"}),
        labels={"namespace": "titan", "service": "api", "level": "error"},
    )
    _install(api, FakeLoki([entry]))
    item = api["client"].get("/api/v1/logs", headers=api["token_for"]("admin")).json()["items"][0]
    # The label is what the stream was selected BY, so a level filter that
    # matched this stream must agree with what the line reports.
    assert item["level"] == "error"
