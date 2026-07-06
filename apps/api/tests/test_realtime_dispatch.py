"""Cross-replica fan-out DoD (Phase 24), hermetic. Two ``ConnectionManager`` +
``WsSubscriber`` pairs share one fakeredis — i.e. two api replicas — and we prove
that a single ``WsFanout.handle_envelope`` reaches a subscribed socket on BOTH
replicas, and that a user-scoped notification reaches only the target user. Runs
in one asyncio loop (fakeredis pub/sub delivers to every subscriber in-loop), so
no real Redis or sockets are needed to exercise the whole publish→subscribe→
socket path."""

import asyncio
import datetime as dt
import uuid
from typing import Any

import fakeredis.aioredis

from titan.events.envelope import Envelope
from titan.events.streams import STREAM_EVENTS_DOMAIN
from titan.realtime import channels
from titan.realtime.fanout import WsFanout
from titan.realtime.manager import Connection, ConnectionManager
from titan.realtime.subscriber import WsSubscriber

from .conftest import run_async

_PID = str(uuid.uuid4())


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        pass


def _env(payload: dict[str, Any]) -> Envelope:
    return Envelope(
        event_id=str(uuid.uuid4()),
        topic=STREAM_EVENTS_DOMAIN,
        occurred_at=dt.datetime(2026, 7, 6, tzinfo=dt.UTC),
        actor="test",
        payload=payload,
    )


async def _pump(subscriber: WsSubscriber, expected: int) -> None:
    """Drain the subscriber's pub/sub until it has dispatched ``expected`` frames
    (bounded so a missing message fails fast instead of hanging)."""
    delivered = 0
    for _ in range(50):
        message = await subscriber._pubsub.get_message(  # noqa: SLF001 — test drives the unit
            ignore_subscribe_messages=True, timeout=0.05
        )
        if message is not None:
            delivered += await subscriber.dispatch(message)
        if delivered >= expected:
            return


def test_task_update_reaches_both_replicas() -> None:
    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        fanout = WsFanout(redis, org="titan")

        # Two replicas, each with its own manager + subscriber over the SAME redis.
        replicas = []
        for _ in range(2):
            mgr = ConnectionManager()
            sub = WsSubscriber(redis, mgr)
            sub._pubsub = redis.pubsub()  # noqa: SLF001
            await sub._pubsub.psubscribe(*channels.SUBSCRIBE_PATTERNS)  # noqa: SLF001
            replicas.append((mgr, sub))
        for _ in range(6):  # drain psubscribe confirmations across both
            for _mgr, sub in replicas:
                await sub._pubsub.get_message(timeout=0.02)  # noqa: SLF001

        socks = []
        conns = []
        for mgr, _sub in replicas:
            sock = _Recorder()
            conn = Connection(sock, user_id="u")
            conn.start_writer()
            mgr.register(conn)
            mgr.subscribe_project(conn, _PID, [])
            socks.append(sock)
            conns.append(conn)

        # ONE fan-out publish.
        await fanout.handle_envelope(
            _env({"type": "task.updated", "task": {"id": "t", "project_id": _PID}})
        )
        for _mgr, sub in replicas:
            await _pump(sub, expected=1)
        await asyncio.sleep(0.05)  # let each connection's writer drain

        # Both replicas' sockets received the frame — cross-replica fan-out.
        for sock in socks:
            assert len(sock.sent) == 1
            assert '"type":"task.updated"' in sock.sent[0]
        for conn in conns:
            await conn.close()
        await redis.aclose()

    run_async(scenario)


def test_notification_reaches_only_target_user() -> None:
    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        fanout = WsFanout(redis, org="titan")
        mgr = ConnectionManager()
        sub = WsSubscriber(redis, mgr)
        sub._pubsub = redis.pubsub()  # noqa: SLF001
        await sub._pubsub.psubscribe(*channels.SUBSCRIBE_PATTERNS)  # noqa: SLF001
        for _ in range(4):
            await sub._pubsub.get_message(timeout=0.02)  # noqa: SLF001

        alice_sock, bob_sock = _Recorder(), _Recorder()
        alice, bob = Connection(alice_sock, user_id="alice"), Connection(bob_sock, user_id="bob")
        for conn in (alice, bob):
            conn.start_writer()
            mgr.register(conn)

        await fanout.handle_envelope(
            _env(
                {
                    "type": "notification.created",
                    "user_id": "alice",
                    "notification": {"id": "n", "title": "hi"},
                }
            )
        )
        await _pump(sub, expected=1)
        await asyncio.sleep(0.05)

        assert len(alice_sock.sent) == 1 and '"type":"notification.created"' in alice_sock.sent[0]
        assert bob_sock.sent == []  # isolation — bob never sees alice's notification
        for conn in (alice, bob):
            await conn.close()
        await redis.aclose()

    run_async(scenario)
