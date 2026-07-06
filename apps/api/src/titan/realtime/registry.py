"""Per-user WebSocket connection cap (Vol 3 §12: 5 concurrent per user).

The cap is enforced across api replicas via a Redis sorted set keyed per user,
``ws.conns.{userId}``: member = connection id, score = lease-expiry epoch. A live
connection refreshes its lease on every heartbeat; a crashed one's lease simply
lapses, so a slot is never leaked (no DECR to lose on a crash).

Admission uses **add-then-check-then-self-remove**: prune expired members, add
self, and keep the slot only if the post-add count is within the cap — otherwise
remove self and reject. The cap can never be exceeded: if more than ``cap``
connections were held, at least one of them would have observed a count over the
cap at its own check (Redis executes each command atomically) and removed itself.
Concurrent connects from different replicas are therefore safe without a Lua
script (fakeredis, used in unit tests, has no EVAL). Occasionally two exactly
simultaneous connects can both back off when one slot was free — fail-safe, and
the client reconnects.
"""

from typing import Final

from redis.asyncio import Redis

from titan.realtime import channels

MAX_CONNECTIONS_PER_USER: Final = 5
#: How long a lease is valid without a heartbeat refresh (> 2 missed 25 s pings).
LEASE_SECONDS: Final = 90


class ConnectionRegistry:
    """Redis-backed connection-slot accounting for the per-user cap."""

    def __init__(
        self, redis: Redis, *, cap: int = MAX_CONNECTIONS_PER_USER, lease: int = LEASE_SECONDS
    ) -> None:
        self._redis = redis
        self._cap = cap
        self._lease = lease

    async def acquire(self, user_id: str, conn_id: str, now: float) -> bool:
        """Try to claim a slot for ``conn_id``. Returns False (and removes itself)
        if the user is already at the cap."""
        key = channels.conns_key(user_id)
        await self._redis.zremrangebyscore(key, 0, now)  # drop lapsed leases
        await self._redis.zadd(key, {conn_id: now + self._lease})
        # A short TTL backstop so an all-crashed user's key eventually vanishes.
        await self._redis.expire(key, self._lease + 60)
        if int(await self._redis.zcard(key)) > self._cap:
            await self._redis.zrem(key, conn_id)
            return False
        return True

    async def refresh(self, user_id: str, conn_id: str, now: float) -> None:
        """Extend a held slot's lease (called on each heartbeat). ``XX`` so it
        never resurrects a slot already released/reclaimed elsewhere."""
        key = channels.conns_key(user_id)
        await self._redis.zadd(key, {conn_id: now + self._lease}, xx=True)
        await self._redis.expire(key, self._lease + 60)

    async def release(self, user_id: str, conn_id: str) -> None:
        """Free a slot on clean disconnect (a crash relies on lease expiry)."""
        await self._redis.zrem(channels.conns_key(user_id), conn_id)


__all__ = ["LEASE_SECONDS", "MAX_CONNECTIONS_PER_USER", "ConnectionRegistry"]
