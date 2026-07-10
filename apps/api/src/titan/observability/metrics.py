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

# Queue observability (Vol 3 §12, TITAN-BCK-011).
STREAM_DLQ_DEPTH = Gauge(
    "titan_stream_dlq_depth",
    "Dead-letter entries per stream (poison messages)",
    ["stream"],
)

STREAM_DEPTH = Gauge(
    "titan_stream_depth",
    "Pending (unacked) entries per stream consumer group",
    ["stream", "group"],
)

# Memory-ingestion lag: Memory Event commit → chunk searchable in Qdrant
# (Vol 5 §5.4, TITAN-BRN-007: p95 ≤ 5 s at ≤ 10 events/s). Buckets bracket the
# 5 s target on both sides so the p95 estimate is meaningful at the threshold.
MEMORY_INGEST_LAG = Histogram(
    "titan_memory_ingest_lag_seconds",
    "Seconds from Memory Event commit to the chunk confirmed searchable in Qdrant",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 30.0, 60.0),
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
