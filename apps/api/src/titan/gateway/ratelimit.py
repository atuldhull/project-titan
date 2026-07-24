"""Redis token-bucket rate limiting (Phase 70; TITAN-BCK-022 + TITAN-SEC-080).

One atomic Lua script (``SCRIPT LOAD`` once at startup, ``EVALSHA`` per request)
implements a token bucket: a key holds ``{tokens, ts}``; each call refills
``rate`` tokens per second up to ``capacity`` (the burst ceiling), spends one,
and reports how many remain and when it refills. Atomicity is the whole point —
a read-modify-write in Python would race under concurrency and leak requests
past the limit.

**Layering (SEC-080).** Every request is checked against, in order:

* a per-IP bucket (pre-auth, 60/min) — the only bucket an unauthenticated
  caller has, so a login flood is capped before a principal exists;
* a per-user bucket (600/min) when authenticated;
* a per-org bucket (per-deployment; single-org here);
* a per-route-class bucket keyed to the caller (read/write/transition/auth/
  project-creation) with the Vol 3 §10 limits.

A request is allowed only if EVERY bucket allows it; the 429 reports the
tightest (soonest-to-reset) bucket. The ``X-RateLimit-*`` headers describe the
most-constrained bucket and are stamped on BOTH 2xx and 429 (BCK-022).

**Internal agent traffic bypasses everything** — the platform must never
rate-limit itself into deadlock (agents are governed by the LLM budget and
WORKER_CONCURRENCY instead).

**Fail-open on a Redis outage**, deliberately and like the login throttle
(``identity.throttle``): a cache blip must not become a total API outage. The
outage is logged; the request proceeds without headers.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

from titan.gateway.client_ip import resolve_client_ip
from titan.gateway.problems import Problem, problem_response, problem_type
from titan.gateway.route_guard import api_routes

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger("titan.gateway.ratelimit")

# KEYS[1] = bucket key
# ARGV = capacity, refill_rate (tokens/s), now (epoch s), requested, ttl (s)
# Returns {allowed(0|1), remaining, retry_after_s, reset_s}. All Lua numbers are
# coerced to RESP integers, so the floats we compute are floored on return.
BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
local delta = now - ts
if delta < 0 then delta = 0 end
tokens = math.min(capacity, tokens + delta * rate)
local allowed = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
end
redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', key, ttl)
local retry = 0
if allowed == 0 and rate > 0 then
  retry = math.ceil((requested - tokens) / rate)
end
local reset = 0
if rate > 0 then
  reset = math.ceil((capacity - tokens) / rate)
end
return {allowed, math.floor(tokens), retry, reset}
"""

# Route classes (SEC-080 / Vol 3 §10 route-class table).
CLASS_READ = "read"
CLASS_WRITE = "write"
CLASS_TRANSITION = "transition"
CLASS_AUTH = "auth"
CLASS_PROJECT_CREATE = "project_create"

# Coarse layered buckets.
LAYER_IP = "ip"
LAYER_USER = "user"
LAYER_ORG = "org"

# Probes/observability are never rate-limited (a limiter that can 429 /readyz
# would take the deployment down under its own health checks).
_EXCLUDED_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/api/v1/healthz",
        "/api/v1/readyz",
        "/metrics",
        "/api/docs",
        "/api/openapi.json",
        "/docs/oauth2-redirect",
    }
)

_AUTH_PREFIXES = ("/api/v1/oauth", "/api/v1/auth")


@dataclass(frozen=True)
class Limit:
    """A token-bucket limit: ``count`` requests per ``window`` seconds, with a
    ``burst`` bucket capacity. Refill is ``count / window`` tokens per second."""

    count: int
    window_seconds: int
    burst: int

    @property
    def rate(self) -> float:
        return self.count / self.window_seconds if self.window_seconds else 0.0

    @property
    def ttl_seconds(self) -> int:
        # Long enough that an idle bucket refills fully before it expires, so a
        # returning caller is never wrongly throttled by a half-full stale key.
        return max(self.window_seconds * 2, 60)


def parse_limit(spec: str) -> Limit:
    """Parse ``"count/window/burst"`` (e.g. ``"300/60/60"``)."""
    parts = spec.split("/")
    if len(parts) != 3:
        raise ValueError(f"rate-limit spec {spec!r} must be 'count/window/burst'")
    count, window, burst = (int(p) for p in parts)
    if count <= 0 or window <= 0 or burst <= 0:
        raise ValueError(f"rate-limit spec {spec!r} values must be positive")
    return Limit(count=count, window_seconds=window, burst=burst)


@dataclass(frozen=True)
class Decision:
    """The outcome for one bucket check (or the tightest of several)."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int
    reset: int

    def headers(self) -> dict[str, str]:
        h = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(self.reset),
        }
        if not self.allowed:
            h["Retry-After"] = str(max(1, self.retry_after))
        return h


# An audit sink: (source_ip, user_id, detail) -> None. Injected at the
# composition root so the gateway does not import identity (module boundary).
AuditHook = Callable[..., Awaitable[None]]


class RateLimiter:
    """Layered Redis token-bucket limiter. One instance per app."""

    def __init__(
        self,
        redis: Redis,
        *,
        classes: dict[str, Limit],
        layers: dict[str, Limit],
        trusted_proxies: list[str],
        org: str,
        audit: AuditHook | None = None,
    ) -> None:
        self._redis = redis
        self._classes = classes
        self._layers = layers
        self._trusted_proxies = trusted_proxies
        self._org = org
        self._audit = audit
        self._script: Any = None  # redis-py AsyncScript (auto-reloads on NOSCRIPT)

    @classmethod
    def build(
        cls,
        redis: Redis,
        *,
        read: str,
        write: str,
        transition: str,
        auth: str,
        project_create: str,
        per_ip: str,
        per_user: str,
        per_org: str,
        trusted_proxies: list[str],
        org: str,
        audit: AuditHook | None = None,
    ) -> RateLimiter:
        """Construct from the ``count/window/burst`` spec strings (settings)."""
        return cls(
            redis,
            classes={
                CLASS_READ: parse_limit(read),
                CLASS_WRITE: parse_limit(write),
                CLASS_TRANSITION: parse_limit(transition),
                CLASS_AUTH: parse_limit(auth),
                CLASS_PROJECT_CREATE: parse_limit(project_create),
            },
            layers={
                LAYER_IP: parse_limit(per_ip),
                LAYER_USER: parse_limit(per_user),
                LAYER_ORG: parse_limit(per_org),
            },
            trusted_proxies=trusted_proxies,
            org=org,
            audit=audit,
        )

    async def load(self) -> None:
        """Register the bucket script (call in the lifespan). register_script is a
        client-side helper — it does not touch Redis until first use, and reloads
        automatically on NOSCRIPT — so there is nothing to await here."""
        self._script = self._redis.register_script(BUCKET_SCRIPT)

    async def _consume(self, key: str, limit: Limit, now: float) -> Decision:
        """Run one bucket. Fails OPEN (returns allowed) on a real Redis outage.

        Uses the redis-py Script helper (``register_script``), which EVALSHAs and
        transparently reloads the script on NOSCRIPT — so a routine Redis restart
        (which drops the cached script) self-heals instead of failing open
        forever on the stale sha (Phase-70 review)."""
        try:
            if self._script is None:
                await self.load()
            assert self._script is not None
            raw = await self._script(
                keys=[key],
                args=[str(limit.burst), str(limit.rate), str(now), "1", str(limit.ttl_seconds)],
            )
            allowed, remaining, retry, reset = (int(v) for v in raw)
            return Decision(
                allowed=bool(allowed),
                limit=limit.count,
                remaining=remaining,
                retry_after=retry,
                reset=reset,
            )
        except Exception:  # pragma: no cover - exercised via the fail-open test
            logger.warning("ratelimit.redis_unavailable", key=key)
            return Decision(
                allowed=True,
                limit=limit.count,
                remaining=limit.burst,
                retry_after=0,
                reset=0,
            )

    async def check(self, buckets: list[tuple[str, Limit]]) -> Decision:
        """Consume one token from every bucket; deny if any denies.

        Returns the tightest decision: the first denial (soonest Retry-After),
        else the allowed bucket with the fewest tokens remaining — that is the
        one whose headers most honestly describe how close the caller is.
        """
        now = time.time()
        results = [await self._consume(key, limit, now) for key, limit in buckets]
        denied = [d for d in results if not d.allowed]
        if denied:
            return max(denied, key=lambda d: d.retry_after)
        if not results:
            return Decision(allowed=True, limit=0, remaining=0, retry_after=0, reset=0)
        return min(results, key=lambda d: d.remaining)

    async def audit_block(self, *, route_class: str, ip: str, user_id: object, path: str) -> None:
        """Record a rate-limit block — best-effort and deduped.

        Best-effort: a failing audit sink (or a Redis blip) must NEVER fail the
        429 the caller is already getting. Deduped: under a sustained flood,
        writing one audit row per blocked request would amplify the DoS into the
        audit store, so we ``SET NX`` a per-(class, IP) marker with a short TTL
        and audit only the first block in that window.
        """
        if self._audit is None:
            return
        try:
            first = await self._redis.set(f"rl:audited:{route_class}:{ip}", "1", nx=True, ex=60)
            if not first:
                return
            await self._audit(
                source_ip=ip,
                user_id=user_id,
                detail={"route_class": route_class, "path": path},
            )
        except Exception:  # pragma: no cover - audit is never a request dependency
            logger.warning("ratelimit.audit_failed", route_class=route_class, ip=ip)

    def buckets_for(
        self, *, route_class: str, ip: str, principal_key: str | None, internal: bool = False
    ) -> list[tuple[str, Limit]]:
        """The (key, limit) buckets that apply to this request.

        ``internal`` marks agent (platform) traffic: it BYPASSES the per-route
        limits AND the per-IP bucket (agents share the worker's socket IP, which
        would otherwise throttle the whole fleet), but is STILL bound by the
        coarse per-user and per-org caps. The old code fully bypassed on
        ``kind=='agent'`` — an attacker-selectable signal (any user can mint an
        API key), so that was an unlimited-flood hole (Phase-70 review).
        """
        buckets: list[tuple[str, Limit]] = []
        # Per-org (per-deployment) layer — always applies, to everyone.
        if LAYER_ORG in self._layers:
            buckets.append((f"rl:org:{self._org}", self._layers[LAYER_ORG]))
        # Per-user layer — whenever a principal is known (users AND agents).
        if principal_key is not None and LAYER_USER in self._layers:
            buckets.append((f"rl:user:{principal_key}", self._layers[LAYER_USER]))
        if internal:
            return buckets  # agents skip per-IP + per-route-class only
        # Per-IP pre-auth layer — the only bucket an unauthenticated caller has.
        if LAYER_IP in self._layers:
            buckets.append((f"rl:ip:{ip}", self._layers[LAYER_IP]))
        # Per-route-class bucket. Auth is keyed per-IP (pre-auth brute-force
        # guard); every other class is keyed to the caller (principal else IP).
        limit = self._classes.get(route_class)
        if limit is not None:
            if route_class == CLASS_AUTH:
                buckets.append((f"rl:cls:auth:{ip}", limit))
            else:
                subject = principal_key or ip
                buckets.append((f"rl:cls:{route_class}:{subject}", limit))
        return buckets


def classify(method: str, permission: str | None, path: str) -> str:
    """Map a route to its SEC-080 rate class."""
    if any(path.startswith(p) for p in _AUTH_PREFIXES) and method not in ("GET", "HEAD"):
        return CLASS_AUTH
    if permission == "project:create":
        return CLASS_PROJECT_CREATE
    if permission is not None and (
        permission.endswith(":transition") or permission == "run:create"
    ):
        return CLASS_TRANSITION
    if method in ("GET", "HEAD"):
        return CLASS_READ
    return CLASS_WRITE


def _route_permission(route: APIRoute) -> str | None:
    """The permission string of the route's Requires guard, if any."""
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return None
    stack = list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        if getattr(dep.call, "__titan_requires__", False):
            perm = getattr(dep.call, "permission", None)
            if perm is not None:
                return str(perm)
        stack.extend(dep.dependencies)
    return None


def build_route_class_table(app: FastAPI) -> dict[tuple[str, str], str]:
    """Walk the built app once and map ``(METHOD, path_template) -> class``.

    Route class is only knowable from the route's ``Requires`` permission, which
    is a per-route dependency invisible at middleware time; resolving it here at
    startup (like the route-guard census) lets the middleware do a cheap lookup.
    Uses ``api_routes`` (not ``app.router.routes``, which holds included-router
    WRAPPERS, not leaf routes — iterating the top level yields zero APIRoutes).
    """
    table: dict[tuple[str, str], str] = {}
    for route in api_routes(app):
        permission = _route_permission(route)
        for method in route.methods or ():
            table[(method, route.path)] = classify(method, permission, route.path)
    return table


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Enforce the layered token buckets and stamp ``X-RateLimit-*`` headers.

    Replaces the reserved ``RateLimitSlot`` pass-through. It sits in the same
    §9.1 position (inside auth, so ``request.state.principal`` is resolved;
    outside idempotency) but — unlike the slot — post-processes the downstream
    response so the headers reach 2xx replies, not just 429s.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
        if limiter is None or request.url.path in _EXCLUDED_PATHS:
            return await call_next(request)

        principal = getattr(request.state, "principal", None)
        # Internal agent traffic bypasses per-route + per-IP limits (never
        # rate-limit the platform into deadlock) but is STILL bound by the coarse
        # per-user/per-org caps — kind=='agent' is attacker-selectable, so a full
        # bypass would be an unlimited-flood hole (Phase-70 review).
        internal = principal is not None and getattr(principal, "kind", None) == "agent"

        ip = resolve_client_ip(request, limiter._trusted_proxies)
        principal_key = str(principal.sub) if principal is not None else None
        table: dict[tuple[str, str], str] = getattr(request.app.state, "route_class_table", {})
        routes: list[APIRoute] = getattr(request.app.state, "rate_limit_routes", [])
        route_class = _resolve_class(request, table, routes)

        decision = await limiter.check(
            limiter.buckets_for(
                route_class=route_class, ip=ip, principal_key=principal_key, internal=internal
            )
        )
        if not decision.allowed:
            await limiter.audit_block(
                route_class=route_class,
                ip=ip,
                user_id=principal.sub if principal is not None else None,
                path=request.url.path,
            )
            logger.info(
                "ratelimit.blocked",
                route_class=route_class,
                path=request.url.path,
                ip=ip,
                retry_after=decision.retry_after,
            )
            return problem_response(
                request,
                Problem(
                    type=problem_type("rate-limited"),
                    title="Rate limit exceeded",
                    status=429,
                    detail="Too many requests; retry after the indicated delay.",
                ),
                headers=decision.headers(),
            )

        response = await call_next(request)
        for name, value in decision.headers().items():
            response.headers[name] = value
        return response


def _resolve_class(
    request: Request, table: dict[tuple[str, str], str], routes: list[APIRoute]
) -> str:
    """Match the request to a leaf route and look up its class (fallback by method).

    ``routes`` are the flat leaf APIRoutes (from ``api_routes`` at startup); the
    matched route's full-path template keys the startup class table.
    """
    for route in routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            cls = table.get((request.method, route.path))
            if cls is not None:
                return cls
    return CLASS_READ if request.method in ("GET", "HEAD") else CLASS_WRITE


__all__ = [
    "BUCKET_SCRIPT",
    "Decision",
    "Limit",
    "RateLimitMiddleware",
    "RateLimiter",
    "build_route_class_table",
    "classify",
    "parse_limit",
]
