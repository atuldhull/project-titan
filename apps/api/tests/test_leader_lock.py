"""Phase 79 follow-up (Vol 9 §8.1): the Postgres session-scoped advisory-lock leader
election primitive that lets the worker's singleton loops run in exactly one replica.
Proves mutual exclusion, release-frees, liveness, and campaign takeover — the failover
path a surviving replica takes when the leader dies.

Each scenario runs inside ONE event loop (via run_async of a single coroutine): a
LeaderLock holds its DB connection across calls, which cannot cross asyncio.run boundaries.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from titan.runtime.leader import LeaderLock, campaign

from .conftest import run_async

pytestmark = pytest.mark.schema

# a test-only advisory key, distinct from WORKER_LEADER_LOCK_KEY and the orchestrator's
_KEY = 0x7469_7461_6C6F_636B  # "titalock"


def test_exclusive_and_release_frees(migrated_engine: Any, schema_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(schema_url, poolclass=NullPool)
        try:
            first, second = LeaderLock(engine, _KEY), LeaderLock(engine, _KEY)
            assert await first.try_acquire() is True
            assert first.is_leader
            assert await second.try_acquire() is False  # first holds it
            await first.release()
            assert await second.try_acquire() is True  # now free
            await second.release()
        finally:
            await engine.dispose()

    run_async(scenario)


def test_reacquire_is_idempotent_and_alive_tracks_holding(
    migrated_engine: Any, schema_url: str
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(schema_url, poolclass=NullPool)
        try:
            lock = LeaderLock(engine, _KEY)
            assert await lock.try_acquire() is True
            assert await lock.try_acquire() is True  # idempotent
            assert await lock.alive() is True
            await lock.release()
            assert lock.is_leader is False
            assert await lock.alive() is False
        finally:
            await engine.dispose()

    run_async(scenario)


def test_campaign_elects_then_stops(migrated_engine: Any, schema_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(schema_url, poolclass=NullPool)
        try:
            stop, elected = asyncio.Event(), asyncio.Event()

            async def on_elected(_lock: LeaderLock) -> None:
                elected.set()
                await stop.wait()  # hold leadership until told to stand down

            task = asyncio.create_task(
                campaign(engine, on_elected, stop=stop, key=_KEY, poll_seconds=0.05)
            )
            await asyncio.wait_for(elected.wait(), timeout=5)
            stop.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            await engine.dispose()

    run_async(scenario)


def test_campaign_takes_over_after_holder_releases(migrated_engine: Any, schema_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(schema_url, poolclass=NullPool)
        try:
            holder = LeaderLock(engine, _KEY)
            assert await holder.try_acquire() is True

            stop, elected = asyncio.Event(), asyncio.Event()

            async def on_elected(_lock: LeaderLock) -> None:
                elected.set()
                await stop.wait()

            task = asyncio.create_task(
                campaign(engine, on_elected, stop=stop, key=_KEY, poll_seconds=0.05)
            )
            await asyncio.sleep(0.3)
            assert not elected.is_set()  # can't win while holder has it
            await holder.release()  # free it → campaign wins on the next poll
            await asyncio.wait_for(elected.wait(), timeout=5)
            stop.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            await engine.dispose()

    run_async(scenario)
