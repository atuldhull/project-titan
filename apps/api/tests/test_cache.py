"""TITAN-BCK-023 read-through cache unit tests: version-bump invalidation and
fail-open behaviour, driven against fakeredis with a tiny FastAPI-like stub so
the pure cache logic is covered without a full app."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
from pydantic import BaseModel

from titan.gateway import cache


class _Doc(BaseModel):
    n: int


def _request(redis: Any, *, cache_enabled: bool = True) -> Any:
    settings = SimpleNamespace(cache_enabled=cache_enabled)
    app = SimpleNamespace(state=SimpleNamespace(redis=redis, settings=settings))
    return SimpleNamespace(app=app, headers={})


def test_read_through_caches_then_bump_invalidates() -> None:
    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        request = _request(redis)
        calls = {"n": 0}

        async def loader() -> _Doc:
            calls["n"] += 1
            return _Doc(n=calls["n"])

        async def read() -> _Doc:
            return await cache.cached_value(
                request,
                resource="project",
                group="doc:1",
                version_key="cache:ver:doc:1",
                ttl_seconds=60,
                loader=loader,
                model=_Doc,
            )

        first = await read()
        second = await read()
        assert first.n == 1 and second.n == 1  # second served from cache (loader not re-run)

        await cache.bump_version(redis, "cache:ver:doc:1")
        third = await read()
        assert third.n == 2  # invalidated → loader ran again

    asyncio.run(scenario())


def test_cache_fails_open_when_redis_errors() -> None:
    async def scenario() -> None:
        class _BrokenRedis:
            async def get(self, *_a: object) -> object:
                raise RuntimeError("redis down")

            async def set(self, *_a: object, **_k: object) -> object:
                raise RuntimeError("redis down")

            async def incr(self, *_a: object) -> object:
                raise RuntimeError("redis down")

        request = _request(_BrokenRedis())

        async def loader() -> _Doc:
            return _Doc(n=7)

        value = await cache.cached_value(
            request,
            resource="project",
            group="doc:1",
            version_key="cache:ver:doc:1",
            ttl_seconds=60,
            loader=loader,
            model=_Doc,
        )
        assert value.n == 7  # Redis outage → straight to Postgres, no crash
        await cache.bump_version(_BrokenRedis(), "k")  # type: ignore[arg-type]  # must not raise

    asyncio.run(scenario())


def test_domain_consumer_invalidates_board_on_task_event() -> None:
    """BCK-023 (Phase-70 review): a task.* domain event — the signal an
    AUTONOMOUS transition emits — bumps the project cache version, so the board
    invalidates even when no REST write happened."""
    from titan.events.consumers import _invalidate_board_cache, _project_id_from_payload

    # project_id nested under `task` (task.created/updated shape) and top-level.
    assert _project_id_from_payload({"task": {"project_id": "p1"}}) == "p1"
    assert _project_id_from_payload({"project_id": "p2"}) == "p2"
    assert _project_id_from_payload({"type": "sprint.updated"}) is None

    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        vkey = cache.project_version_key("p1")
        before = await redis.get(vkey)

        class _Env:
            payload = {"type": "task.updated", "task": {"project_id": "p1"}}

        class _Delivery:
            envelope = _Env()

        await _invalidate_board_cache(redis, _Delivery())  # type: ignore[arg-type]
        after = await redis.get(vkey)
        assert after is not None and after != before  # version advanced

        # A non-task event must NOT bump.
        class _Env2:
            payload = {"type": "agent.status", "project_id": "p1"}

        class _Delivery2:
            envelope = _Env2()

        await _invalidate_board_cache(redis, _Delivery2())  # type: ignore[arg-type]
        assert await redis.get(vkey) == after  # unchanged

    asyncio.run(scenario())


def test_cache_disabled_bypasses_redis() -> None:
    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        request = _request(redis, cache_enabled=False)
        calls = {"n": 0}

        async def loader() -> _Doc:
            calls["n"] += 1
            return _Doc(n=calls["n"])

        for _ in range(3):
            await cache.cached_value(
                request,
                resource="project",
                group="doc:1",
                version_key="cache:ver:doc:1",
                ttl_seconds=60,
                loader=loader,
                model=_Doc,
            )
        assert calls["n"] == 3  # never cached → loader every time

    asyncio.run(scenario())
