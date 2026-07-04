"""Prometheus metrics: RED middleware + /metrics endpoint (Vol 3 §12).

The request-duration histogram uses the ROUTE TEMPLATE (``/api/v1/projects``,
not raw paths) so label cardinality stays bounded; unmatched requests collapse
into a single ``unmatched`` route label.
"""

import time

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response as StarletteResponse
from starlette.routing import Route

REQUEST_DURATION = Histogram(
    "titan_http_request_duration_seconds",
    "HTTP request duration by route template (RED, Vol 3 §12)",
    ["method", "route", "status"],
)

READYZ_DEPENDENCY_UP = Gauge(
    "titan_readyz_dependency_up",
    "1 when the dependency passed its last /readyz check, 0 when it failed",
    ["dependency"],
)

router = APIRouter()

_FIXED_PATHS = frozenset({"/api/docs", "/api/openapi.json", "/metrics"})


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


class REDMetricsMiddleware(BaseHTTPMiddleware):
    """Observe every request into the RED histogram, keyed by route template."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> StarletteResponse:
        started = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            route = request.scope.get("route")
            if isinstance(route, Route):
                template = route.path
            elif request.url.path in _FIXED_PATHS:
                # Starlette-level routes (docs, OpenAPI) never set scope["route"];
                # they are fixed paths, so labeling them adds no cardinality.
                template = request.url.path
            else:
                template = "unmatched"
            REQUEST_DURATION.labels(method=request.method, route=template, status=status).observe(
                time.perf_counter() - started
            )
