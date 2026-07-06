"""Per-project sequence + replay buffer (Vol 3 §5 reconnect semantics).

Each project event is assigned a monotonic ``seq`` (``INCR ws.seq.{pid}``) and
the buffered ones are appended to a capped Redis list ``ws.buffer.{pid}`` (last
≤1,000, TTL 15 min). On reconnect the client sends ``subscribe`` with
``last_seq``:

* ``last_seq`` at or past the newest buffered seq → nothing to replay (``[]``).
* ``last_seq`` still covered by the buffer → replay every buffered frame with
  ``seq > last_seq``, in order, before live events.
* ``last_seq`` older than the oldest buffered frame (the gap can't be filled) →
  :data:`RESYNC`, telling the endpoint to send ``sys.resync`` so the client
  refetches via REST.

``seq`` is per-project (not per-connection) so it survives reconnects and is
identical across every connection watching the project — which is what makes
``last_seq`` a stable cross-reconnect cursor. A single connection still observes
a strictly increasing ``seq``, satisfying the §5 "per-connection monotonic"
statement. Ephemeral frames (``run.output.delta``) still consume a project seq
for ordering but are not buffered (they are never replayed).
"""

import json
from typing import Any, Final

from redis.asyncio import Redis

from titan.realtime import channels

MAX_BUFFER: Final = 1000
BUFFER_TTL_SECONDS: Final = 15 * 60

#: Sentinel returned by :func:`replay` when the gap is beyond the buffer window.
RESYNC: Final = object()


class ProjectBuffer:
    """Sequence assignment + replay against one Redis instance."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def next_project_seq(self, project_id: str) -> int:
        return int(await self._redis.incr(channels.project_seq_key(project_id)))

    async def append(self, project_id: str, frame_wire: str) -> None:
        """Buffer a fully-formed (seq-stamped) project frame; cap + refresh TTL.

        RPUSH keeps oldest→newest ordering; LTRIM to the last MAX_BUFFER bounds
        it; EXPIRE refreshes the 15-min window on every write (an active project
        keeps its buffer; an idle one lets it lapse)."""
        key = channels.buffer_key(project_id)
        pipe = self._redis.pipeline()
        pipe.rpush(key, frame_wire)
        pipe.ltrim(key, -MAX_BUFFER, -1)
        pipe.expire(key, BUFFER_TTL_SECONDS)
        await pipe.execute()

    async def replay(self, project_id: str, last_seq: int) -> "list[dict[str, Any]] | object":
        """Return the ordered gap after ``last_seq``, ``[]`` if caught up, or
        :data:`RESYNC` if the gap is older than the buffer window."""
        entries = await self._redis.lrange(channels.buffer_key(project_id), 0, -1)
        frames = [json.loads(e) for e in entries]
        if not frames:
            # Nothing buffered: caught up iff last_seq already covers the counter,
            # else the gap has expired out of the window → resync.
            current = int(await self._redis.get(channels.project_seq_key(project_id)) or 0)
            return [] if last_seq >= current else RESYNC
        # Sort by seq defensively: the single fan-out consumer appends in seq order,
        # but a resync/retry could still leave the list unsorted, and replay MUST be
        # ordered by seq (positional order is not trusted).
        frames.sort(key=lambda f: f["seq"])
        newest = frames[-1]["seq"]
        oldest = frames[0]["seq"]
        if last_seq >= newest:
            return []
        if last_seq + 1 < oldest:
            return RESYNC  # a gap the buffer no longer covers
        return [f for f in frames if f["seq"] > last_seq]


__all__ = ["BUFFER_TTL_SECONDS", "MAX_BUFFER", "RESYNC", "ProjectBuffer"]
