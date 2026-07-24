"""Endpoint lifecycle tests (Phase 24) driving ``serve_connection`` directly with
an in-memory socket double — deterministic and single-loop, so heartbeat/revocation
timing is controllable (via a shrunk ``HEARTBEAT_SECONDS``). The connection-cap,
revoke-close, missed-pong-close, in-band re-auth, ping/pong and protocol-error
paths need no database; the subscribe/replay/resync paths hit Postgres for the
Phase-9 RBAC check and are ``schema``-marked."""

import asyncio
import json
import types
import uuid
from collections.abc import Coroutine
from typing import Any, cast

import fakeredis.aioredis
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.db import uuid7
from titan.identity.service import Principal
from titan.identity.tokens import KeyRing
from titan.realtime import endpoint, protocol
from titan.realtime.buffer import ProjectBuffer
from titan.realtime.channels import conns_key
from titan.realtime.manager import ConnectionManager
from titan.settings import Settings

from .conftest import _test_jwt_key_b64, run_async

_DISCONNECT = object()


class _FakeWS:
    """The slice of a Starlette WebSocket ``serve_connection`` touches."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.sent: list[str] = []
        self.closed: tuple[int, str | None] | None = None
        self._incoming: asyncio.Queue[Any] = asyncio.Queue()

    async def accept(self) -> None:
        pass

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        item = await self._incoming.get()
        if item is _DISCONNECT:
            from starlette.websockets import WebSocketDisconnect

            raise WebSocketDisconnect(1000)
        return str(item)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)

    def feed(self, frame: dict[str, Any]) -> None:
        self._incoming.put_nowait(json.dumps(frame))

    def hangup(self) -> None:
        self._incoming.put_nowait(_DISCONNECT)

    def frames(self) -> list[dict[str, Any]]:
        return [json.loads(s) for s in self.sent]


def _keyring() -> KeyRing:
    return KeyRing(
        Settings(
            jwt_private_key_b64=_test_jwt_key_b64(), otel_exporter_otlp_endpoint="", _env_file=None
        )
    )


def _app(redis: Any, keyring: KeyRing, engine: Any = None) -> Any:
    return types.SimpleNamespace(
        state=types.SimpleNamespace(
            redis=redis,
            keyring=keyring,
            engine=engine,
            ws_manager=ConnectionManager(),
            ws_org="titan",
            # Phase 70: the endpoint reads the WS caps (connection/msg-rate) off
            # settings; the ws_audit hook is optional (getattr → no-op if absent).
            settings=Settings(otel_exporter_otlp_endpoint="", _env_file=None),
        )
    )


def _fake_redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _principal(
    keyring: KeyRing, roles: list[str], sub: uuid.UUID | None = None
) -> tuple[Principal, str]:
    sub = sub or uuid7()
    token, jti = keyring.mint(sub=str(sub), roles=roles, scope="user")
    return Principal(sub=sub, kind="user", roles=roles, scope="user", jti=jti), token


def _serve(ws: _FakeWS, principal: Principal, token: str) -> Coroutine[Any, Any, None]:
    """serve_connection with the socket double (cast past the WebSocket type)."""
    return endpoint.serve_connection(cast(Any, ws), principal, token)


async def _wait_for(ws: _FakeWS, predicate: Any, within: float = 2.0) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        for frame in ws.frames():
            if predicate(frame):
                return frame
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected frame never sent; got {ws.frames()}")


# --- no-database lifecycle paths -------------------------------------------


def test_over_cap_connection_is_rejected() -> None:
    async def scenario() -> None:
        redis = _fake_redis()
        keyring = _keyring()
        principal, token = _principal(keyring, ["viewer"])
        # Pre-fill the user's 5 slots so this connection is the 6th.
        for i in range(5):
            await redis.zadd(conns_key(str(principal.sub)), {f"existing{i}": 1e12})
        app = _app(redis, keyring)
        ws = _FakeWS(app)
        await _serve(ws, principal, token)  # returns immediately
        assert ws.closed is not None and ws.closed[0] == 1008
        assert any(f["payload"].get("code") == "connection_cap" for f in ws.frames())
        assert app.state.ws_manager.count() == 0  # never registered
        await redis.aclose()

    run_async(scenario)


def test_ping_pong_and_protocol_error() -> None:
    async def scenario() -> None:
        redis = _fake_redis()
        keyring = _keyring()
        principal, token = _principal(keyring, ["viewer"])
        ws = _FakeWS(_app(redis, keyring))
        task = asyncio.create_task(_serve(ws, principal, token))
        ws.feed({"type": "ping"})
        await _wait_for(ws, lambda f: f["type"] == protocol.PONG)
        ws._incoming.put_nowait("{not valid json")
        await _wait_for(
            ws, lambda f: f["type"] == protocol.ERROR and f["payload"]["code"] == "bad_frame"
        )
        ws.hangup()
        await asyncio.wait_for(task, timeout=2.0)
        await redis.aclose()

    run_async(scenario)


def test_revocation_closes_the_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(endpoint, "HEARTBEAT_SECONDS", 0.05)

    async def scenario() -> None:
        redis = _fake_redis()
        keyring = _keyring()
        principal, token = _principal(keyring, ["viewer"])
        ws = _FakeWS(_app(redis, keyring))
        task = asyncio.create_task(_serve(ws, principal, token))
        await asyncio.sleep(0.02)
        await redis.set(f"deny:jti:{principal.jti}", "1")  # TITAN-SEC-016 revocation
        await _wait_for(ws, lambda f: f["payload"].get("code") == "token_expired")
        await asyncio.wait_for(task, timeout=2.0)
        assert ws.closed is not None and ws.closed[0] == 1008
        await redis.aclose()

    run_async(scenario)


def test_missed_pongs_close_the_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(endpoint, "HEARTBEAT_SECONDS", 0.03)

    async def scenario() -> None:
        redis = _fake_redis()
        keyring = _keyring()
        principal, token = _principal(keyring, ["viewer"])
        ws = _FakeWS(_app(redis, keyring))
        task = asyncio.create_task(_serve(ws, principal, token))
        # Never answer the server pings → closed after 2 missed pongs.
        await asyncio.wait_for(task, timeout=2.0)
        assert ws.closed is not None and ws.closed[0] == 1001
        await redis.aclose()

    run_async(scenario)


def test_in_band_reauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(endpoint, "HEARTBEAT_SECONDS", 100.0)  # keep heartbeat out of the way

    async def scenario() -> None:
        redis = _fake_redis()
        keyring = _keyring()
        principal, token = _principal(keyring, ["viewer"])
        ws = _FakeWS(_app(redis, keyring))
        task = asyncio.create_task(_serve(ws, principal, token))
        # A fresh token for the SAME subject is accepted.
        fresh, _ = keyring.mint(sub=str(principal.sub), roles=["viewer"], scope="user")
        ws.feed({"type": "authenticate", "token": fresh})
        await _wait_for(ws, lambda f: f["type"] == protocol.AUTHENTICATED)
        # A token for a DIFFERENT subject is rejected (identity mismatch).
        other, _ = keyring.mint(sub=str(uuid7()), roles=[], scope="user")
        ws.feed({"type": "authenticate", "token": other})
        await _wait_for(ws, lambda f: f["payload"].get("code") == "auth_failed")
        ws.hangup()
        await asyncio.wait_for(task, timeout=2.0)
        await redis.aclose()

    run_async(scenario)


# --- subscribe / replay / resync (need Postgres for the RBAC check) --------


async def _seed_role(engine: AsyncEngine, name: str, perms: list[str]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO roles (id, name, permissions) VALUES (:id, :n, CAST(:p AS jsonb)) "
                "ON CONFLICT (name) DO UPDATE SET permissions = CAST(:p AS jsonb)"
            ),
            {"id": uuid7(), "n": name, "p": json.dumps(perms)},
        )


@pytest.mark.schema
def test_subscribe_authorized_and_unauthorized(migrated_engine: AsyncEngine) -> None:
    async def scenario() -> None:
        redis = _fake_redis()
        keyring = _keyring()
        await _seed_role(migrated_engine, "viewer", ["*:read"])
        pid = str(uuid7())

        # A viewer (org-wide *:read) may subscribe → ``subscribed`` ack.
        viewer, vtoken = _principal(keyring, ["viewer"])
        ws = _FakeWS(_app(redis, keyring, migrated_engine))
        task = asyncio.create_task(_serve(ws, viewer, vtoken))
        ws.feed({"type": "subscribe", "project_id": pid, "topics": ["tasks"]})
        ack = await _wait_for(ws, lambda f: f["type"] == protocol.SUBSCRIBED)
        assert ack["payload"]["project_id"] == pid
        ws.hangup()
        await asyncio.wait_for(task, timeout=2.0)

        # A principal with no roles has no visibility → typed not_found (no leak).
        nobody, ntoken = _principal(keyring, [])
        ws2 = _FakeWS(_app(redis, keyring, migrated_engine))
        task2 = asyncio.create_task(_serve(ws2, nobody, ntoken))
        ws2.feed({"type": "subscribe", "project_id": pid})
        err = await _wait_for(ws2, lambda f: f["type"] == protocol.ERROR)
        assert err["payload"]["code"] == "not_found"
        ws2.hangup()
        await asyncio.wait_for(task2, timeout=2.0)
        await redis.aclose()

    run_async(scenario)


@pytest.mark.schema
def test_reconnect_replays_gap_then_resyncs(migrated_engine: AsyncEngine) -> None:
    async def scenario() -> None:
        redis = _fake_redis()
        keyring = _keyring()
        await _seed_role(migrated_engine, "viewer", ["*:read"])
        pid = str(uuid7())
        buf = ProjectBuffer(redis)
        for i in range(1, 4):  # buffer seqs 1,2,3
            seq = await buf.next_project_seq(pid)
            await buf.append(
                pid,
                protocol.encode(
                    protocol.build_frame("task.updated", seq=seq, payload={"i": i}, project_id=pid)
                ),
            )

        # A second project whose buffer has been trimmed to seqs 90..92 (an old
        # last_seq can no longer be replayed from it).
        trimmed = str(uuid7())
        await redis.set(f"ws.seq.{trimmed}", 92)
        for seq in (90, 91, 92):
            await buf.append(
                trimmed,
                protocol.encode(
                    protocol.build_frame("task.updated", seq=seq, payload={}, project_id=trimmed)
                ),
            )

        viewer, vtoken = _principal(keyring, ["viewer"])
        ws = _FakeWS(_app(redis, keyring, migrated_engine))
        task = asyncio.create_task(_serve(ws, viewer, vtoken))

        # Reconnect with last_seq=1 → replay 2,3 in order BEFORE the subscribed ack.
        ws.feed({"type": "subscribe", "project_id": pid, "last_seq": 1})
        ack = await _wait_for(ws, lambda f: f["type"] == protocol.SUBSCRIBED)
        replayed = [
            f for f in ws.frames() if f["type"] == "task.updated" and f["project_id"] == pid
        ]
        assert [f["seq"] for f in replayed] == [2, 3]
        assert ws.frames().index(ack) > ws.frames().index(replayed[-1])  # ack after replay

        # A last_seq behind the trimmed window → sys.resync (client refetches).
        ws.feed({"type": "subscribe", "project_id": trimmed, "last_seq": 10})
        await _wait_for(ws, lambda f: f["type"] == protocol.SYS_RESYNC)
        ws.hangup()
        await asyncio.wait_for(task, timeout=2.0)
        await redis.aclose()

    run_async(scenario)


@pytest.mark.schema
def test_org_activity_requires_org_wide_read(migrated_engine: AsyncEngine) -> None:
    async def scenario() -> None:
        redis = _fake_redis()
        keyring = _keyring()
        await _seed_role(migrated_engine, "viewer", ["*:read"])

        # Org-wide reader (viewer) may join the org activity feed.
        viewer, vtoken = _principal(keyring, ["viewer"])
        ws = _FakeWS(_app(redis, keyring, migrated_engine))
        task = asyncio.create_task(_serve(ws, viewer, vtoken))
        ws.feed({"type": "subscribe", "topics": ["org:activity"]})
        await _wait_for(ws, lambda f: f["type"] == protocol.SUBSCRIBED)
        assert not any(f["type"] == protocol.ERROR for f in ws.frames())
        ws.hangup()
        await asyncio.wait_for(task, timeout=2.0)

        # A principal without org-wide read is REFUSED the org feed (no cross-project
        # leak) — the critical RBAC-bypass fix.
        nobody, ntoken = _principal(keyring, [])
        ws2 = _FakeWS(_app(redis, keyring, migrated_engine))
        task2 = asyncio.create_task(_serve(ws2, nobody, ntoken))
        ws2.feed({"type": "subscribe", "topics": ["org:activity"]})
        err = await _wait_for(ws2, lambda f: f["type"] == protocol.ERROR)
        assert err["payload"]["code"] == "forbidden"
        ws2.hangup()
        await asyncio.wait_for(task2, timeout=2.0)
        await redis.aclose()

    run_async(scenario)


@pytest.mark.schema
def test_subscription_cap_bounds_resolver_lookups(
    migrated_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(endpoint, "MAX_PROJECT_SUBSCRIPTIONS", 3)

    async def scenario() -> None:
        redis = _fake_redis()
        keyring = _keyring()
        await _seed_role(migrated_engine, "viewer", ["*:read"])
        viewer, vtoken = _principal(keyring, ["viewer"])
        ws = _FakeWS(_app(redis, keyring, migrated_engine))
        task = asyncio.create_task(_serve(ws, viewer, vtoken))
        # Three distinct projects are accepted; the fourth is refused (bounding the
        # per-connection resolver cache + DB lookups — anti-DoS).
        for _ in range(3):
            ws.feed({"type": "subscribe", "project_id": str(uuid7())})
        ws.feed({"type": "subscribe", "project_id": str(uuid7())})
        err = await _wait_for(ws, lambda f: f["payload"].get("code") == "subscription_limit")
        assert err["type"] == protocol.ERROR
        ws.hangup()
        await asyncio.wait_for(task, timeout=2.0)
        await redis.aclose()

    run_async(scenario)
