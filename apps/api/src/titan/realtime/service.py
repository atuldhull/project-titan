"""Public surface of the `realtime` module (Vol 3 §3, TITAN-BCK-007).

The composition roots (``main.py`` for the api, ``worker.py`` for the fan-out
consumer) build the WS backbone through these factories: a per-replica
:class:`ConnectionManager` + :class:`WsSubscriber` (pub/sub → local sockets) and
the :class:`WsFanout` consumer (domain stream → pub/sub channels).
"""

from redis.asyncio import Redis

from titan.realtime.buffer import ProjectBuffer
from titan.realtime.fanout import (
    WS_FANOUT_GROUP,
    WsFanout,
    process_ws_fanout_batch,
    run_ws_fanout,
)
from titan.realtime.manager import Connection, ConnectionManager
from titan.realtime.registry import ConnectionRegistry
from titan.realtime.subscriber import WsSubscriber


def build_manager() -> ConnectionManager:
    """A fresh per-replica connection registry."""
    return ConnectionManager()


def build_fanout(redis: Redis, *, org: str) -> WsFanout:
    """The domain-stream → WS-channels republisher for this process."""
    return WsFanout(redis, org=org)


def build_subscriber(redis: Redis, manager: ConnectionManager) -> WsSubscriber:
    """The per-replica pub/sub → local-socket forwarder."""
    return WsSubscriber(redis, manager)


__all__ = [
    "WS_FANOUT_GROUP",
    "Connection",
    "ConnectionManager",
    "ConnectionRegistry",
    "ProjectBuffer",
    "WsFanout",
    "WsSubscriber",
    "build_fanout",
    "build_manager",
    "build_subscriber",
    "process_ws_fanout_batch",
    "run_ws_fanout",
]
