"""Race-safe task-claim locks (TITAN-BCK-015): ``lock:task:{id}`` via SET NX PX.

The scheduler acquires this before the ``ready -> assigned`` transition so two
workers racing for one task produce exactly one winner. Acquire is atomic
(SET NX); release and heartbeat-renew are token-guarded WATCH/MULTI transactions
so a claimer never deletes or extends a lock it no longer holds (e.g. after its
TTL lapsed and another worker took over). No Lua — portable across the real
Redis client (decode_responses=True) and fakeredis in tests.
"""

from redis.asyncio import Redis
from redis.exceptions import WatchError

from titan.db import uuid7

#: Default hold time; the assign transition is quick, but a worker running the
#: task heartbeat-renews this until the run finishes (Phase 14).
LOCK_TTL_MS = 30_000


def _key(task_id: object) -> str:
    return f"lock:task:{task_id}"


async def acquire(redis: Redis, task_id: object, *, ttl_ms: int = LOCK_TTL_MS) -> str | None:
    """SET NX PX. Returns a unique hold token on success, None if already held."""
    token = uuid7().hex
    acquired = await redis.set(_key(task_id), token, nx=True, px=ttl_ms)
    return token if acquired else None


async def _guarded(redis: Redis, key: str, token: str, apply: object) -> bool:
    """Run ``apply(pipe)`` in a MULTI/EXEC only while ``key`` still holds
    ``token`` (optimistic WATCH). Returns whether it ran."""
    async with redis.pipeline() as pipe:
        while True:
            try:
                await pipe.watch(key)
                if await pipe.get(key) != token:
                    await pipe.reset()  # type: ignore[no-untyped-call]
                    return False
                pipe.multi()  # type: ignore[no-untyped-call]
                apply(pipe)  # type: ignore[operator]
                await pipe.execute()
                return True
            except WatchError:
                continue


async def release(redis: Redis, task_id: object, token: str) -> bool:
    """Delete the lock only if we still hold ``token`` (atomic)."""
    key = _key(task_id)
    return await _guarded(redis, key, token, lambda pipe: pipe.delete(key))


async def renew(redis: Redis, task_id: object, token: str, *, ttl_ms: int = LOCK_TTL_MS) -> bool:
    """Heartbeat: extend the TTL only while we still hold ``token``."""
    key = _key(task_id)
    return await _guarded(redis, key, token, lambda pipe: pipe.pexpire(key, ttl_ms))
