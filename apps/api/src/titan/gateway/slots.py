"""Replaceable gateway slots for authentication and RBAC.

The §9.1 middleware order reserves these positions now; Phases 8/9 plug real
strategies in by assigning ``app.state.auth_strategy`` / ``rbac_strategy``. A
strategy is an async callable ``(request) -> Response | None`` — return a
Response to short-circuit, None to continue. Unset strategies pass through.

Rate limiting (TITAN-BCK-022) no longer uses a slot: it must stamp
``X-RateLimit-*`` headers onto the DOWNSTREAM 2xx response, which the
short-circuit-only slot contract cannot do, so it is a dedicated middleware —
:class:`titan.gateway.ratelimit.RateLimitMiddleware` (Phase 70).
"""

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

Strategy = Callable[[Request], Awaitable[Response | None]]


class _SlotMiddleware(BaseHTTPMiddleware):
    state_attr: str = ""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        strategy: Strategy | None = getattr(request.app.state, self.state_attr, None)
        if strategy is not None:
            short_circuit = await strategy(request)
            if short_circuit is not None:
                return short_circuit
        return await call_next(request)


class AuthenticationSlot(_SlotMiddleware):
    """Filled by Phase 8 (local JWTs, API keys, OIDC in M7)."""

    state_attr = "auth_strategy"


class RBACSlot(_SlotMiddleware):
    """Filled by Phase 9 (Requires("resource:action") + role resolution)."""

    state_attr = "rbac_strategy"
