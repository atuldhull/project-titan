"""Per-replica Redis pub/sub subscriber → local sockets (TITAN-BCK-007).

Each api replica runs ONE subscriber that pattern-subscribes to every WS channel
and routes each received frame to the matching local connections via the
:class:`~titan.realtime.manager.ConnectionManager`. This is the receive half of
the cross-replica fan-out: the :mod:`~titan.realtime.fanout` consumer publishes
once, and every replica's subscriber delivers to its own sockets — so two clients
on two replicas both get the frame.

The channel a message arrived on tells us who it is for: ``ws.project.{id}`` →
that project's subscribers, ``notifications:{userId}`` → that user's sockets,
``agents:status`` → all, ``org:{oid}:activity`` → that org's opt-ins. The frame's
``type`` (parsed out of the wire) drives per-connection topic filtering.

``start()`` only spawns the loop; the (re)subscribe happens *inside* it and
self-heals on a Redis hiccup, so a transient/unavailable Redis at boot never
fails app startup — it simply reconnects when Redis returns.
"""

import asyncio
import contextlib
import json
from typing import Any

import structlog
from redis.asyncio import Redis

from titan.realtime import channels
from titan.realtime.manager import ConnectionManager

logger = structlog.get_logger("titan.realtime")


class WsSubscriber:
    """Owns one pub/sub connection and dispatches to the local manager."""

    def __init__(self, redis: Redis, manager: ConnectionManager) -> None:
        self._redis = redis
        self._manager = manager
        self._pubsub: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        """Spawn the receive loop. Never touches Redis synchronously, so a Redis
        that is briefly unavailable at boot does not block startup."""
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._close_pubsub()

    async def _run(self) -> None:  # pragma: no cover — loop; unit tests call dispatch()
        while not self._stopped:
            try:
                if self._pubsub is None:
                    self._pubsub = self._redis.pubsub()
                    await self._pubsub.psubscribe(*channels.SUBSCRIBE_PATTERNS)
                    await self._pubsub.subscribe(*channels.SUBSCRIBE_CHANNELS)
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ws.subscriber.recv_error")
                await self._close_pubsub()  # force a fresh (re)subscribe next loop
                self._pubsub = None
                await asyncio.sleep(0.5)
                continue
            if message is not None:
                await self.dispatch(message)

    async def _close_pubsub(self) -> None:
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()

    async def dispatch(self, message: dict[str, Any]) -> int:
        """Route one pub/sub message to local sockets. Returns how many sockets
        it was delivered to (the deterministic unit the tests drive)."""
        if message.get("type") not in ("message", "pmessage"):
            return 0
        channel = message["channel"]
        wire = message["data"]
        frame_type = _frame_type(wire)
        if channel == channels.AGENTS_STATUS_CHANNEL:
            return await self._manager.dispatch_agents(wire)
        if channel.startswith("ws.project."):
            return await self._manager.dispatch_project(
                channel.removeprefix("ws.project."), frame_type, wire
            )
        if channel.startswith("notifications:"):
            return await self._manager.dispatch_user(channel.removeprefix("notifications:"), wire)
        if channel.startswith("org:") and channel.endswith(":activity"):
            org = channel[len("org:") : -len(":activity")]
            return await self._manager.dispatch_org(org, wire)
        return 0


def _frame_type(wire: str) -> str:
    try:
        parsed = json.loads(wire)
        return str(parsed.get("type", ""))
    except (json.JSONDecodeError, TypeError):
        return ""


__all__ = ["WsSubscriber"]
