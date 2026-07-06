"""Hermetic unit tests for the realtime primitives (Phase 24): protocol parsing,
the per-project replay buffer, the connection-cap registry, and the local
connection manager. Everything runs in a single asyncio loop over fakeredis or
pure objects — no Postgres, no real Redis, no sockets."""

import asyncio
import uuid

import fakeredis.aioredis
import pytest

from titan.realtime import channels, protocol
from titan.realtime.buffer import RESYNC, ProjectBuffer
from titan.realtime.manager import Connection, ConnectionManager
from titan.realtime.registry import ConnectionRegistry

from .conftest import run_async


def _fake() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


# --- protocol --------------------------------------------------------------


def test_build_frame_uses_fixed_envelope() -> None:
    frame = protocol.build_frame("task.updated", seq=7, payload={"a": 1}, project_id="p1")
    assert set(frame) == {"v", "type", "seq", "ts", "project_id", "payload"}
    assert frame["v"] == 1 and frame["seq"] == 7 and frame["project_id"] == "p1"
    assert frame["ts"].endswith("Z")


def test_parse_subscribe_frame_variants() -> None:
    pid = uuid.uuid4()
    frame = protocol.parse_client_frame(
        f'{{"type":"subscribe","project_id":"{pid}","topics":["tasks"],"last_seq":42}}'
    )
    assert isinstance(frame, protocol.SubscribeFrame)
    assert frame.project_id == pid and frame.topics == ["tasks"] and frame.last_seq == 42
    # ``op`` alias (Vol 4 §5.1) and project-less subscribe both parse.
    assert isinstance(protocol.parse_client_frame('{"op":"sub"}'), protocol.SubscribeFrame)


def test_parse_ping_unsub_authenticate() -> None:
    assert isinstance(protocol.parse_client_frame('{"type":"ping"}'), protocol.PingFrame)
    assert isinstance(protocol.parse_client_frame('{"type":"pong"}'), protocol.PongFrame)
    assert isinstance(
        protocol.parse_client_frame('{"type":"unsubscribe","project_id":null}'),
        protocol.UnsubscribeFrame,
    )
    auth = protocol.parse_client_frame('{"type":"authenticate","token":"abc"}')
    assert isinstance(auth, protocol.AuthenticateFrame) and auth.token == "abc"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1,2,3]",
        '{"type":"subscribe","project_id":"not-a-uuid"}',
        '{"type":"subscribe","topics":"tasks"}',
        '{"type":"subscribe","last_seq":"x"}',
        '{"type":"authenticate"}',
        '{"type":"bogus"}',
        "{}",
    ],
)
def test_parse_rejects_malformed(raw: str) -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_client_frame(raw)


def test_error_frame_is_control() -> None:
    frame = protocol.error_frame("forbidden", "nope", project_id="p1")
    assert frame["type"] == protocol.ERROR and frame["seq"] == 0
    assert frame["payload"] == {"code": "forbidden", "message": "nope"}


# --- ProjectBuffer: seq + replay/resync ------------------------------------


def test_buffer_seq_is_monotonic_and_replays_gap() -> None:
    async def scenario() -> None:
        redis = _fake()
        buf = ProjectBuffer(redis)
        pid = "proj"
        seqs = []
        for i in range(5):
            seq = await buf.next_project_seq(pid)
            seqs.append(seq)
            wire = protocol.encode(protocol.build_frame("task.updated", seq=seq, payload={"i": i}))
            await buf.append(pid, wire)
        assert seqs == [1, 2, 3, 4, 5]  # strictly increasing
        # Reconnect with last_seq=2 → replay 3,4,5 in order.
        replayed = await buf.replay(pid, 2)
        assert [f["seq"] for f in replayed] == [3, 4, 5]  # type: ignore[union-attr]
        # Caught up.
        assert await buf.replay(pid, 5) == []
        await redis.aclose()

    run_async(scenario)


def test_buffer_resync_beyond_window() -> None:
    async def scenario() -> None:
        redis = _fake()
        buf = ProjectBuffer(redis)
        pid = "proj"
        # Simulate a trimmed buffer: seq counter at 100 but only 90..92 retained.
        await redis.set(channels.project_seq_key(pid), 92)
        for seq in (90, 91, 92):
            await redis.rpush(
                channels.buffer_key(pid),
                protocol.encode(protocol.build_frame("task.updated", seq=seq, payload={})),
            )
        # last_seq older than the oldest retained (90) → resync.
        assert await buf.replay(pid, 10) is RESYNC
        # Empty buffer, last_seq behind the counter → resync.
        await redis.delete(channels.buffer_key(pid))
        assert await buf.replay(pid, 10) is RESYNC
        # Empty buffer, last_seq at the counter → caught up.
        assert await buf.replay(pid, 92) == []
        await redis.aclose()

    run_async(scenario)


# --- ConnectionRegistry: per-user cap --------------------------------------


def test_registry_caps_at_five() -> None:
    async def scenario() -> None:
        redis = _fake()
        reg = ConnectionRegistry(redis, cap=5, lease=90)
        user = "u1"
        acquired = [await reg.acquire(user, f"c{i}", now=1000.0) for i in range(7)]
        assert acquired == [True, True, True, True, True, False, False]
        assert await redis.zcard(channels.conns_key(user)) == 5
        # Releasing frees a slot for a new connection.
        await reg.release(user, "c0")
        assert await reg.acquire(user, "c7", now=1000.0) is True
        await redis.aclose()

    run_async(scenario)


def test_registry_reclaims_expired_leases() -> None:
    async def scenario() -> None:
        redis = _fake()
        reg = ConnectionRegistry(redis, cap=2, lease=90)
        assert await reg.acquire("u", "a", now=1000.0) is True
        assert await reg.acquire("u", "b", now=1000.0) is True
        assert await reg.acquire("u", "c", now=1000.0) is False  # full
        # 200 s later a's + b's leases (expiry 1090) have lapsed → slot frees.
        assert await reg.acquire("u", "c", now=1300.0) is True
        await redis.aclose()

    run_async(scenario)


# --- ConnectionManager: local routing --------------------------------------


class _Recorder:
    """A Socket double that records every frame written to it."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: tuple[int, str | None] | None = None

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)


async def _drain(*conns: Connection) -> None:
    """Start each connection's writer and let it flush its queue to the socket."""
    for conn in conns:
        conn.start_writer()
    for _ in range(20):
        await asyncio.sleep(0.005)


def test_manager_routes_by_project_with_topic_filter() -> None:
    async def scenario() -> None:
        mgr = ConnectionManager()
        a, b = _Recorder(), _Recorder()
        ca = Connection(a, user_id="ua")
        cb = Connection(b, user_id="ub")
        for c in (ca, cb):
            c.start_writer()
            mgr.register(c)
        mgr.subscribe_project(ca, "p1", ["tasks"])  # only task frames
        mgr.subscribe_project(cb, "p1", [])  # all frames

        assert await mgr.dispatch_project("p1", "task.updated", "TASKFRAME") == 2
        # A run frame is filtered out for ``ca`` (topic filter) but not ``cb``.
        assert await mgr.dispatch_project("p1", "run.started", "RUNFRAME") == 1
        await asyncio.sleep(0.05)
        assert a.sent == ["TASKFRAME"]  # filtered
        assert b.sent == ["TASKFRAME", "RUNFRAME"]  # queue is FIFO — order preserved
        # Unsubscribe removes the connection from the project index.
        mgr.unsubscribe_project(cb, "p1")
        assert await mgr.dispatch_project("p1", "task.updated", "X") == 1
        await ca.close()
        await cb.close()

    run_async(scenario)


def test_manager_user_and_agents_scopes() -> None:
    async def scenario() -> None:
        mgr = ConnectionManager()
        a, b = _Recorder(), _Recorder()
        ca = Connection(a, user_id="alice")
        cb = Connection(b, user_id="bob")
        mgr.register(ca)
        mgr.register(cb)
        # A notification for alice reaches only alice's socket.
        assert await mgr.dispatch_user("alice", "NOTE") == 1
        # agents:status is global (both auto-subscribed on register).
        assert await mgr.dispatch_agents("STATUS") == 2
        await _drain(ca, cb)
        assert a.sent == ["NOTE", "STATUS"] and b.sent == ["STATUS"]
        # After unregister a connection receives nothing more.
        mgr.unregister(ca)
        assert await mgr.dispatch_user("alice", "NOTE2") == 0
        await ca.close()
        await cb.close()

    run_async(scenario)


def test_connection_sheds_a_stalled_reader_via_bounded_queue() -> None:
    async def scenario() -> None:
        from titan.realtime.manager import SEND_QUEUE_MAX

        conn = Connection(_Recorder(), user_id="u")  # no writer started → nothing drains
        for _ in range(SEND_QUEUE_MAX):
            conn.enqueue("x")
        assert conn.alive is True
        conn.enqueue("overflow")  # queue full → shed the connection (no unbounded growth)
        assert conn.alive is False

    run_async(scenario)


def test_connection_hold_defers_live_frames_until_after_replay() -> None:
    async def scenario() -> None:
        rec = _Recorder()
        conn = Connection(rec, user_id="u")
        conn.start_writer()
        conn.projects["p1"] = set()  # subscribed to all topics
        conn.begin_hold("p1")
        # A live frame published DURING the hold is captured, not sent yet.
        assert conn.deliver_project("p1", "task.updated", "LIVE") is True
        conn.enqueue("REPLAY-1")  # the buffered gap is enqueued first
        conn.enqueue("REPLAY-2")
        conn.end_hold("p1")  # now flush the held live frame — after the gap
        await asyncio.sleep(0.05)
        assert rec.sent == ["REPLAY-1", "REPLAY-2", "LIVE"]
        await conn.close()

    run_async(scenario)
