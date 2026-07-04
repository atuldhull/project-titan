"""Gateway middleware: request identity and the structured access log.

Issues a UUIDv7 request id per request (or honors an inbound ``X-Request-ID``),
binds it to structlog contextvars so every log line in the request carries it
(TITAN-BCK-024), echoes it on the response, and emits one ``http.request``
event per request — uvicorn's plaintext access log stays disabled.
"""

import time

import structlog
import uuid_utils
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger("titan.gateway")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind request-scoped logging context and emit the access log line."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid_utils.uuid7())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.error(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=500,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)

        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        return response
