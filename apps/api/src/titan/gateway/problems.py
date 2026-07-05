"""RFC 9457 Problem Details — the only error shape the API speaks (§4.1).

Every error response is ``application/problem+json`` with
``{type, title, status, detail, instance, errors[]}``. Domain exceptions
subclass :class:`ProblemException` (e.g. the orchestrator's
DependencyCycleError) so the generic handler renders them — the gateway, being
shared infrastructure, never imports a domain module.
"""

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_CONTENT_TYPE = "application/problem+json"
_TYPE_BASE = "https://titan.dev/errors"

# Framework HTTPException statuses → titan.dev type slugs, so a 404/405/415 the
# router raises carries a machine-routable type like every domain error.
_HTTP_TYPE_SLUGS: dict[int, str] = {
    404: "not-found",
    405: "method-not-allowed",
    406: "not-acceptable",
    415: "unsupported-media-type",
}


class Problem(BaseModel):
    """RFC 9457 body; ``errors`` carries field-level details on 422."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[dict[str, Any]] = Field(default_factory=list)


class ProblemException(Exception):
    """Raise anywhere below the gateway to produce a problem+json response."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        problem_type: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.title = title
        self.detail = detail
        self.problem_type = problem_type or "about:blank"
        self.errors = errors or []
        self.headers = headers or {}
        super().__init__(f"{status} {title}: {detail}")


def problem_type(slug: str) -> str:
    return f"{_TYPE_BASE}/{slug}"


def problem_response(
    request: Request, problem: Problem, headers: dict[str, str] | None = None
) -> JSONResponse:
    problem.instance = problem.instance or str(request.url.path)
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def register_problem_handlers(app: FastAPI, production: bool = False) -> None:
    """Install the global exception → problem+json mapping (§4.1 Errors).

    The catch-all handler runs in Starlette's ServerErrorMiddleware —
    architecturally OUTSIDE the §9.1 stack — so it must stamp the
    TITAN-BCK-019 security headers and X-Request-ID itself.
    """

    @app.exception_handler(ProblemException)
    async def on_problem(request: Request, exc: ProblemException) -> JSONResponse:
        return problem_response(
            request,
            Problem(
                type=exc.problem_type,
                title=exc.title,
                status=exc.status,
                detail=exc.detail,
                errors=exc.errors,
            ),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def on_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            request,
            Problem(
                type=problem_type("validation"),
                title="Request validation failed",
                status=422,
                detail="One or more request fields are invalid.",
                errors=[
                    {
                        "loc": list(error.get("loc", [])),
                        "msg": error.get("msg", ""),
                        "kind": error.get("type", ""),
                    }
                    for error in exc.errors()
                ],
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def on_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Give framework-generated errors (unmatched-route 404, method 405,
        # media-type 415, …) a titan.dev/errors/* type slug too, so EVERY error
        # the API emits carries a machine-routable type — not just domain ones.
        return problem_response(
            request,
            Problem(
                type=problem_type(_HTTP_TYPE_SLUGS.get(exc.status_code, "http")),
                title=exc.detail or "HTTP error",
                status=exc.status_code,
            ),
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals; the traceback is in the JSON logs with
        # request_id/trace_id correlation.
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
        }
        if production:
            headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        # The catch-all runs outside the middleware task, where structlog's
        # contextvars may not be visible — fall back to the inbound header.
        request_id = request.headers.get(
            "X-Request-ID"
        ) or structlog.contextvars.get_contextvars().get("request_id")
        if request_id:
            headers["X-Request-ID"] = str(request_id)
        return problem_response(
            request,
            Problem(
                type=problem_type("internal"),
                title="Internal server error",
                status=500,
            ),
            headers=headers,
        )
