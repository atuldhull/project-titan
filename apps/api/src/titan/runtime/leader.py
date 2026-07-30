"""Postgres session-scoped advisory-lock leader election (Vol 9 §8.1, TITAN-OPS-008).

The worker process bundles two kinds of loop: **scalable** Redis consumer-group loops (run
in every replica, each with a unique consumer name) and **singleton** loops — the domain
stream → pub/sub fan-out (a single consumer preserves per-project seq order) and the periodic
DB reconcilers (running N copies is at best wasteful, at worst a race). To let the worker
scale (Phase 80 KEDA) without breaking the singletons, exactly one replica must run them.

:class:`LeaderLock` holds a **dedicated** DB connection and wins `pg_try_advisory_lock` — a
*session*-scoped lock (not the transaction-scoped `pg_advisory_xact_lock` the orchestrator
uses for graph writes). Whoever holds it is the single leader and runs the singleton loops.
When the leader dies its DB session ends and the lock frees, so a surviving replica's
:func:`campaign` loop acquires it — automatic failover with no external coordinator.

This is the primitive; wiring the worker's loops onto it (leader-only vs all-replicas, plus
per-replica consumer names) is the integration step that follows.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = structlog.get_logger("titan.runtime.leader")

# Distinct from the orchestrator's xact dependency-graph key. "titanldr" as a bigint.
WORKER_LEADER_LOCK_KEY = 0x7469_7461_6E6C_6472


class LeaderLock:
    """A single-holder, session-scoped advisory lock. Not concurrency-safe for use from
    multiple tasks on one instance — one instance per campaigning coroutine."""

    def __init__(self, engine: AsyncEngine, key: int = WORKER_LEADER_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._conn: AsyncConnection | None = None

    @property
    def is_leader(self) -> bool:
        return self._conn is not None

    async def try_acquire(self) -> bool:
        """Try to become leader. True if the lock is now held by this instance (on a
        dedicated held connection); False if another holder has it. Idempotent — returns
        True if this instance already holds it. On a DB error the connection is discarded."""
        if self._conn is not None:
            return True
        conn = await self._engine.connect()
        try:
            got = bool(
                (
                    await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key})
                ).scalar_one()
            )
        except BaseException:
            await conn.close()
            raise
        if got:
            self._conn = conn
            return True
        await conn.close()  # someone else holds it; don't tie up a connection
        return False

    async def alive(self) -> bool:
        """True while the lock's connection is healthy (the lock is still ours). If the
        connection has dropped, leadership is LOST — the caller must stop leader work and
        (re-)campaign. Prevents split-brain from a silent connection drop."""
        conn = self._conn
        if conn is None:
            return False
        try:
            await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            self._conn = None
            with contextlib.suppress(Exception):
                await conn.close()
            return False

    async def release(self) -> None:
        """Release the lock. Explicit `pg_advisory_unlock` first so a POOLED connection
        doesn't return to the pool still holding the session lock (a plain close would leak
        it); then close the connection."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        with contextlib.suppress(Exception):
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key})
        with contextlib.suppress(Exception):
            await conn.close()


async def campaign(
    engine: AsyncEngine,
    on_elected: Callable[[LeaderLock], Awaitable[None]],
    *,
    stop: asyncio.Event,
    key: int = WORKER_LEADER_LOCK_KEY,
    poll_seconds: float = 10.0,
) -> None:
    """Campaign for leadership until ``stop``. Poll ``try_acquire`` every ``poll_seconds``;
    once elected, run ``on_elected(lock)`` (which starts the singleton loops and returns when
    ``stop`` is set or leadership is lost), then release. A non-leader keeps polling so it
    takes over when the current leader dies."""
    lock = LeaderLock(engine, key)
    try:
        while not stop.is_set():
            try:
                if await lock.try_acquire():
                    try:
                        await on_elected(lock)  # returns on stop OR leadership loss
                    finally:
                        await lock.release()  # step down before re-contesting
                    continue  # re-check stop; if leadership was merely lost, campaign again
            except asyncio.CancelledError:
                raise
            except Exception:
                # Self-healing (like every worker loop): a transient DB/pool error during
                # acquisition must NOT permanently kill election in this replica — log,
                # discard any half-open connection, and retry on the next poll. Without
                # this, a Postgres restart would silently leave the singleton loops dead
                # until the pod is restarted.
                logger.warning("leader.campaign_error", exc_info=True)
                await lock.release()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
    finally:
        await lock.release()


__all__ = ["WORKER_LEADER_LOCK_KEY", "LeaderLock", "campaign"]
