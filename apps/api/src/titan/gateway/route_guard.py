"""Route-guard introspection (TITAN-BCK-021 / TITAN-SEC-030 acceptance).

Every registered API route MUST either declare a Requires/RequiresAuth guard
or be explicitly marked public. This walks the built app's routes — RECURSING
into included routers and mounts — and returns the unprotected ones so the CI
check (scripts/check_route_guards.py) fails the build on any omission.

FastAPI's ``include_router`` wraps children in a ``_IncludedRouter`` whose
real routes live under ``original_router.routes``; a naive top-level scan sees
zero APIRoutes and is a silent no-op. This walker descends those wrappers,
Starlette ``Mount``s, and flags unguarded WebSocket routes too.
"""

from collections.abc import Iterable, Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute

# Framework/probe routes that legitimately need no guard.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/api/v1/healthz",
        "/api/v1/readyz",
        "/metrics",
        "/api/docs",
        "/api/openapi.json",
        "/docs/oauth2-redirect",
        # Plugin webhooks (Vol 7 §3.2) are INBOUND calls from external systems (Jira,
        # GitHub, …) which cannot present a TITAN JWT. They are authenticated instead by
        # an HMAC-SHA256 signature over the raw body, verified against the plugin's named
        # secret; an unverified payload is rejected 401 and logged. That signature IS the
        # guard, so the route is deliberately public to the JWT layer.
        "/api/v1/plugins/{plugin_id}/hooks/{hook_path:path}",
    }
)


def _child_routes(route: BaseRoute) -> list[BaseRoute] | None:
    """Sub-routes for a container route (included router, Mount), else None."""
    included = getattr(route, "original_router", None)
    if included is not None and hasattr(included, "routes"):
        return list(included.routes)
    if isinstance(route, Mount):
        return list(route.routes)
    return None


def _walk(routes: Iterable[BaseRoute]) -> Iterator[BaseRoute]:
    """Yield every leaf route, descending included routers and mounts."""
    for route in routes:
        children = _child_routes(route)
        if children is not None:
            yield from _walk(children)
        else:
            yield route


def _has_guard(route: APIRoute | APIWebSocketRoute) -> bool:
    """True if any dependency in the route's tree is a titan guard."""
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False
    stack: list[Any] = list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        if getattr(dep.call, "__titan_requires__", False):
            return True
        stack.extend(dep.dependencies)
    return False


def _is_public(route: APIRoute | APIWebSocketRoute) -> bool:
    return route.path in PUBLIC_PATHS or getattr(route.endpoint, "__titan_public__", False)


def unprotected_routes(app: FastAPI) -> list[str]:
    """Return "METHOD path" for every route lacking a guard or public mark."""
    offenders: list[str] = []
    for route in _walk(app.routes):
        if isinstance(route, APIRoute | APIWebSocketRoute):
            if _is_public(route) or _has_guard(route):
                continue
            methods = ",".join(sorted(getattr(route, "methods", None) or {"WS"}))
            offenders.append(f"{methods} {route.path}")
        elif isinstance(route, WebSocketRoute):
            # Bare Starlette WS route (not a FastAPI APIWebSocketRoute).
            if route.path not in PUBLIC_PATHS:
                offenders.append(f"WS {route.path} (unguarded websocket)")
        elif isinstance(route, Route):
            # Framework Starlette routes (docs/openapi) must be allowlisted;
            # anything else is a bare route carrying no dependency machinery.
            if route.path not in PUBLIC_PATHS:
                offenders.append(f"{sorted(route.methods or [])} {route.path} (non-API route)")
    return offenders


def format_report(offenders: Iterable[str]) -> str:
    lines = "\n".join(f"  - {o}" for o in offenders)
    return (
        "Route-guard check FAILED (TITAN-BCK-021): these routes declare no "
        "Requires/RequiresAuth guard and are not marked public:\n" + lines
    )
