"""Security headers, body-size cap, and the production docs guard.

TITAN-BCK-019: nosniff + Referrer-Policy always, HSTS only in production;
/docs (and the OpenAPI document) require auth outside dev mode. §9.1 caps
JSON bodies at 2 MiB — artifacts travel via presigned URLs, never the API.
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from titan.gateway.problems import PROBLEM_CONTENT_TYPE

BODY_CAP_BYTES = 2 * 1024 * 1024
_DOCS_PATHS = {"/api/docs", "/api/openapi.json"}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, production: bool, *, emit_hsts: bool = True) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.production = production
        # HSTS ships in production AND only when the secure default is on
        # (settings.hsts_enabled, TITAN-SEC-140). Turning it off is a deliberate,
        # `titan security check`-visible relaxation, not a silent gap.
        self.emit_hsts = emit_hsts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if self.production and self.emit_hsts:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response


class BodySizeCapMiddleware(BaseHTTPMiddleware):
    """Rejects declared oversize bodies. Chunked requests without a
    Content-Length bypass this check by design at MVP — production ingress
    (Vol 9 K8s NGINX/Traefik) enforces the transport-level cap; revisit if
    the API ever fronts traffic without ingress."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > BODY_CAP_BYTES:
            return JSONResponse(
                status_code=413,
                media_type=PROBLEM_CONTENT_TYPE,
                content={
                    "type": "https://titan.dev/errors/payload-too-large",
                    "title": "Payload too large",
                    "status": 413,
                    "detail": "JSON bodies are capped at 2 MiB; upload artifacts via storage URLs.",
                    "instance": str(request.url.path),
                },
            )
        return await call_next(request)


class DocsAuthGuardMiddleware(BaseHTTPMiddleware):
    """Outside dev, /docs is auth-only (TITAN-BCK-019). Runs inside the auth
    slot: any authenticated principal set by Phase 8 unlocks it."""

    def __init__(self, app: object, production: bool) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.production = production

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            self.production
            and request.url.path in _DOCS_PATHS
            and getattr(request.state, "principal", None) is None
        ):
            return JSONResponse(
                status_code=401,
                media_type=PROBLEM_CONTENT_TYPE,
                content={
                    "type": "https://titan.dev/errors/unauthorized",
                    "title": "Authentication required",
                    "status": 401,
                    "detail": "API documentation requires authentication in production.",
                    "instance": str(request.url.path),
                },
            )
        return await call_next(request)
