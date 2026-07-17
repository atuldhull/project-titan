"""The Loki query port and its HTTP adapter.

A port so the service is testable without a Loki: the observability stack is
``profiles: ["observability"]`` in Compose, so a plain ``docker compose up`` —
and every CI job — has no Loki at all. Tests fake this Protocol; production
binds :class:`HttpLokiQuerier`.

Everything version-specific about Loki 3.3.1 is confined to this file.
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog

logger = structlog.get_logger("titan.logs.loki")

#: Loki's own ceiling: `validation.max-entries-limit` defaults to 5000 and
#: `loki-config.yaml` does not override it. Loki REJECTS a bigger limit with a
#: 400 — it does not clamp — so the router validates against this rather than
#: letting a 5001 turn into a confusing upstream error.
MAX_ENTRIES = 5000

#: The largest page the endpoint offers. One BELOW Loki's ceiling, because the
#: query asks for `limit + 1` as its has-more sentinel: publishing 5000 would
#: mean the endpoint's own documented maximum sent 5001 and 400'd every time.
MAX_PAGE = MAX_ENTRIES - 1

_TIMEOUT_S = 20.0


class LokiUnavailable(RuntimeError):
    """Loki is not configured, or could not be reached."""


class LokiQueryError(ValueError):
    """Loki rejected the query (a 400 — almost always malformed LogQL)."""


@dataclass(frozen=True)
class LokiEntry:
    """One log line as Loki returns it.

    ``ts_ns`` stays a **string**. It is a Unix nanosecond timestamp, so it
    exceeds 2**53 and would lose precision the moment anything parsed it as a
    JS number (or a Python float). It is also the pagination cursor's unit, so
    rounding it would skip or repeat lines at a page boundary.
    """

    ts_ns: str
    line: str
    labels: dict[str, str]


class LokiQuerier(Protocol):
    """Runs one range query against Loki and returns its entries."""

    async def query_range(
        self,
        query: str,
        *,
        start_ns: str,
        end_ns: str,
        limit: int,
        direction: str,
    ) -> list[LokiEntry]: ...


class HttpLokiQuerier:
    """:class:`LokiQuerier` over Loki's HTTP API (tested against 3.3.1).

    ``client`` is required rather than optional-with-a-fallback. Constructing
    an ``AsyncClient`` per call looks harmless and is not: the viewer's tail
    polls at 1 Hz per open tab, so every poll would pay a fresh TCP (and TLS)
    handshake and pool nothing. The app already owns a shared client on
    ``app.state.http``; this takes it.
    """

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def query_range(
        self,
        query: str,
        *,
        start_ns: str,
        end_ns: str,
        limit: int,
        direction: str,
    ) -> list[LokiEntry]:
        params = {
            "query": query,
            "start": start_ns,
            "end": end_ns,
            "limit": str(limit),
            "direction": direction,
        }
        # `step` is a matrix-query parameter and is ignored for a streams query;
        # sending it just invites a 400 on a future version.
        try:
            response = await self._get("/loki/api/v1/query_range", params)
        except httpx.HTTPError as exc:
            # Connect/read/timeout: Loki is down or not deployed. Distinguished
            # from a 400 because one is our fault and the other is not.
            raise LokiUnavailable(f"could not reach Loki: {exc}") from exc

        if response.status_code == 400:
            # Loki serves errors as text/plain, NOT problem+json — calling
            # .json() here would raise and bury the actual message.
            raise LokiQueryError(response.text.strip() or "Loki rejected the query")
        if response.status_code != 200:
            # Everything else — 429 (rate limited), 499 (client closed), 504
            # (deadline exceeded), 5xx — is "Loki could not answer". Carry its
            # body: it is plain text and it is the only thing that says WHICH,
            # and an operator reading a bare "unexpected status 429" learns
            # nothing about the limit they hit.
            detail = response.text.strip()
            raise LokiUnavailable(
                f"Loki returned {response.status_code}" + (f": {detail}" if detail else "")
            )

        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LokiUnavailable("Loki returned a non-JSON body") from exc
        return _parse_streams(payload)

    async def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        return await self._client.get(f"{self._base_url}{path}", params=params, timeout=_TIMEOUT_S)


def _parse_streams(payload: Any) -> list[LokiEntry]:
    """Flatten a ``resultType: streams`` response into entries.

    Defensive throughout: this is a foreign service's JSON, and a shape we did
    not expect must not 500 the endpoint.
    """
    if not isinstance(payload, dict):
        raise LokiUnavailable("Loki response was not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise LokiUnavailable("Loki response had no data object")

    result_type = data.get("resultType")
    if result_type not in (None, "streams"):
        # A metric query (`resultType: matrix`) means the LogQL we built was an
        # aggregation, which has no log lines to return.
        raise LokiQueryError(f"expected a log query, but Loki returned {result_type!r}")

    result = data.get("result")
    if not isinstance(result, list):
        return []

    entries: list[LokiEntry] = []
    for stream in result:
        if not isinstance(stream, dict):
            continue
        raw_labels = stream.get("stream")
        labels = (
            {str(k): str(v) for k, v in raw_labels.items()} if isinstance(raw_labels, dict) else {}
        )
        values = stream.get("values")
        if not isinstance(values, list):
            continue
        for value in values:
            # Loki 3.x appends a third element (structured metadata) when a
            # stream has any, so this unpacks by INDEX rather than asserting a
            # 2-tuple. Our promtail sets none today; an OTLP-ingested stream
            # would, and that must not start throwing.
            if not isinstance(value, list) or len(value) < 2:
                continue
            ts_ns, line = value[0], value[1]
            if not isinstance(ts_ns, str) or not isinstance(line, str):
                continue
            entries.append(LokiEntry(ts_ns=ts_ns, line=line, labels=labels))
    return entries
