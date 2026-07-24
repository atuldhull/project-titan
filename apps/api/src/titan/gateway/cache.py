"""Read-through Redis cache for hot GETs (Phase 70; TITAN-BCK-023).

Postgres is always authoritative; this is a read-through accelerator, never a
dependency. Every Redis call is wrapped so a miss, a corrupt entry, or a total
Redis outage degrades to a plain Postgres read — a cache blip must never 500 the
hottest endpoints.

**Versioned keys, no wildcard deletes (Vol 3 §11).** Each cacheable resource has
a monotonic version counter (``cache:ver:<resource-group>``). A read resolves the
current version first, then reads ``cache:<resource>:v<n>``; a write bumps the
counter with ``INCR``, so the next read misses and every stale ``v<n>`` entry is
simply orphaned to its TTL. This is invalidation without scanning or deleting —
and it is atomic, so there is no populate-after-write window that could pin a
value one version behind.

**Why a dedicated counter, not the row ``version``.** A project row's ``version``
only bumps on a project PATCH; it does NOT move when a task transitions — so
keying the board on it would serve a stale board after every task move. The
board and project DTO share ``cache:ver:project:{id}``, bumped by BOTH project
writes and task writes, so a task transition invalidates the board.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel

from titan.observability.metrics import CACHE_HITS, CACHE_MISSES

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from starlette.requests import Request
    from starlette.responses import Response

logger = structlog.get_logger("titan.gateway.cache")


async def bump_version(redis: Redis, version_key: str) -> None:
    """Invalidate a resource group by advancing its version (INCR). Fail-open:
    a Redis blip just means the next read may serve one stale value until TTL."""
    try:
        await redis.incr(version_key)
    except Exception:  # pragma: no cover - fail-open path
        logger.warning("cache.bump_failed", key=version_key)


async def _current_version(redis: Redis, version_key: str) -> str | None:
    """The resource group's version as a string ('0' when never bumped), or None
    when Redis is unreachable (→ caching is skipped, straight to Postgres)."""
    try:
        value = await redis.get(version_key)
    except Exception:  # pragma: no cover - fail-open path
        logger.warning("cache.version_read_failed", key=version_key)
        return None
    return str(value) if value is not None else "0"


async def _read_through[M: BaseModel](
    redis: Redis,
    resource: str,
    group: str,
    version: str,
    ttl_seconds: int,
    loader: Callable[[], Awaitable[M]],
    model: type[M],
) -> M:
    """The core read-through against ``cache:<group>:v<version>``. Fail-open at
    every Redis touch; a corrupt entry is treated as a miss."""
    cache_key = f"cache:{group}:v{version}"
    try:
        raw = await redis.get(cache_key)
    except Exception:  # pragma: no cover - fail-open path
        raw = None
    if raw is not None:
        try:
            value = model.model_validate_json(raw)
            CACHE_HITS.labels(resource=resource).inc()
            return value
        except Exception:  # corrupt/schema-skewed entry → treat as a miss
            logger.warning("cache.corrupt_entry", key=cache_key)
    CACHE_MISSES.labels(resource=resource).inc()
    value = await loader()
    try:
        await redis.set(cache_key, value.model_dump_json(), ex=ttl_seconds)
    except Exception:  # pragma: no cover - fail-open path
        logger.warning("cache.store_failed", key=cache_key)
    return value


async def cached_value[M: BaseModel](
    request: Request,
    *,
    resource: str,
    group: str,
    version_key: str,
    ttl_seconds: int,
    loader: Callable[[], Awaitable[M]],
    model: type[M],
) -> M:
    """Read-through a resource body WITHOUT touching ETags — for endpoints that
    already carry a row-``version`` ETag for optimistic concurrency (``get_project``
    feeds its ETag to ``If-Match`` on PATCH; a cache-version ETag there would
    break that read-then-write contract). Fails open to a plain Postgres read."""
    if not getattr(request.app.state.settings, "cache_enabled", True):
        return await loader()
    redis: Redis = request.app.state.redis
    version = await _current_version(redis, version_key)
    if version is None:  # Redis down → straight to the loader, no caching.
        return await loader()
    return await _read_through(redis, resource, group, version, ttl_seconds, loader, model)


@dataclass
class CacheOutcome[M: BaseModel]:
    """The result of a cached GET. ``not_modified`` is True when the caller's
    ``If-None-Match`` matched the current ETag → the handler returns a bare 304."""

    value: M | None
    not_modified: bool


async def cached_get[M: BaseModel](
    request: Request,
    response: Response,
    *,
    resource: str,
    group: str,
    version_key: str,
    ttl_seconds: int,
    loader: Callable[[], Awaitable[M]],
    model: type[M],
) -> CacheOutcome[M]:
    """Serve ``resource`` read-through with a cache-version ``ETag`` + ``If-None-Match``
    304 — for GETs with NO optimistic-concurrency ETag contract (board, agents).
    Fails open: any Redis error → load from Postgres, no cache, no ETag.
    """
    if not getattr(request.app.state.settings, "cache_enabled", True):
        return CacheOutcome(await loader(), not_modified=False)
    redis: Redis = request.app.state.redis
    version = await _current_version(redis, version_key)
    if version is None:  # Redis down → straight to the loader, no caching.
        return CacheOutcome(await loader(), not_modified=False)

    etag = f'W/"{group}:{version}"'
    if request.headers.get("if-none-match") == etag:
        response.headers["ETag"] = etag
        return CacheOutcome(None, not_modified=True)

    value = await _read_through(redis, resource, group, version, ttl_seconds, loader, model)
    response.headers["ETag"] = etag
    return CacheOutcome(value, not_modified=False)


def project_version_key(project_id: object) -> str:
    """Shared version counter for a project's DTO AND its board (task writes
    bump it too, so the board invalidates on a transition)."""
    return f"cache:ver:project:{project_id}"


AGENTS_VERSION_KEY = "cache:ver:agents"


__all__ = [
    "AGENTS_VERSION_KEY",
    "CacheOutcome",
    "bump_version",
    "cached_get",
    "project_version_key",
]
