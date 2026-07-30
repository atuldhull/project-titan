"""Phase 80: the in-flight gauges that drive the api / llm-gateway HPAs (Vol 9 §4.3).
The api gauge must actually rise DURING a request and fall back after (a registration-only
test would pass even if the middleware never inc/dec'd)."""

from __future__ import annotations

from typing import Any

from prometheus_client import REGISTRY
from starlette.requests import Request
from starlette.responses import Response as StarletteResponse

from titan.llm.metrics import LLM_CALLS_IN_FLIGHT
from titan.observability.metrics import REDMetricsMiddleware

from .conftest import run_async

pytestmark: list[Any] = []

_HTTP = "titan_http_requests_in_flight"
_LLM = "titan_llm_calls_in_flight"


def _val(name: str) -> float:
    return REGISTRY.get_sample_value(name) or 0.0


async def _noop_app(scope: Any, receive: Any, send: Any) -> None:
    return None


def test_api_middleware_tracks_in_flight() -> None:
    before = _val(_HTTP)
    seen: dict[str, float] = {}

    async def call_next(_request: Request) -> StarletteResponse:
        seen["during"] = _val(_HTTP)
        return StarletteResponse(status_code=200)

    mid = REDMetricsMiddleware(app=_noop_app)  # app unused; dispatch is called directly
    scope = {"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""}

    async def run() -> None:
        await mid.dispatch(Request(scope), call_next)

    run_async(run)
    assert seen["during"] == before + 1  # rose during the request
    assert _val(_HTTP) == before  # fell back after (finally decs on every path)


def test_llm_in_flight_gauge_is_registered_and_balanced() -> None:
    # the gateway's inc/dec around the provider call is exercised by the gateway suite;
    # here assert the gauge is real and usable (the HPA references it by name).
    before = _val(_LLM)
    LLM_CALLS_IN_FLIGHT.inc()
    assert _val(_LLM) == before + 1
    LLM_CALLS_IN_FLIGHT.dec()
    assert _val(_LLM) == before
