"""The ``/ws`` endpoint: handshake auth, connection cap, and the frame loop
(ADR-009, Vol 3 §5/§12, TITAN-SEC-016).

Handshake auth reuses the exact identity machinery REST uses — RS256 verify +
the Redis jti/family denylist (:func:`titan.identity.service.verify_bearer`) — so
there is no parallel WS auth. A bad token is rejected at the handshake; over the
per-user concurrency cap is closed at connect. Subscribing to a project runs the
Phase-9 RBAC check for that project, and joining the org activity feed requires
org-wide read; a failure is a typed ``error`` frame, not a dropped socket. Two
concurrent tasks run the connection: a receive loop for client frames and a
heartbeat loop that pings every 25 s, closes after 2 missed pongs, refreshes the
cap lease, and re-checks token validity every tick so an expired or revoked token
closes the socket in ≤ 60 s unless re-authenticated in-band. All server writes go
through the connection's bounded outbound queue, so no single slow socket can
stall the shared dispatch loop.
"""

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from fastapi import FastAPI, Query, WebSocket, WebSocketException, status
from starlette.websockets import WebSocketDisconnect

from titan.gateway.rbac import PermissionResolver, permission_granted
from titan.identity.service import AuthenticationFailed, Principal, TokenInvalid, verify_bearer
from titan.realtime import protocol
from titan.realtime.buffer import RESYNC, ProjectBuffer
from titan.realtime.manager import Connection, ConnectionManager
from titan.realtime.registry import ConnectionRegistry

logger = structlog.get_logger("titan.realtime")

HEARTBEAT_SECONDS = 25
MAX_MISSED_PONGS = 2
#: Subscribing to a project's stream (or the org feed) requires read access.
SUBSCRIBE_PERMISSION = "project:read"
#: Distinct project subscriptions one connection may request — bounds the
#: per-connection RBAC-resolver cache + DB lookups (anti-DoS).
MAX_PROJECT_SUBSCRIPTIONS = 64
#: When the inbound rate gate is exhausted, sleep this long before reading again
#: — the backpressure that slows a flooder before the disconnect (TITAN-SEC-081).
_BACKPRESSURE_SLEEP = 0.05
#: WS abuse audit event names (mirror titan.identity.audit.SecurityEventType
#: values). realtime does NOT import identity's audit module — the module
#: independence contract only permits the ``.service`` facade — so the record is
#: done by an ``app.state.ws_audit`` hook wired at the composition root (main.py),
#: exactly like the rate-limiter's audit hook.
WS_CONNECTION_REJECTED = "ws.connection_rejected"
WS_FLOOD_DISCONNECTED = "ws.flood_disconnected"


@dataclass
class _MessageRateGate:
    """Per-connection inbound token bucket (TITAN-SEC-081): refills ``rate``
    tokens/second up to ``burst``; a frame arriving with no token is a
    violation. ``over_streak`` accumulates violations and is reset ONLY when the
    bucket has fully refilled — i.e. the client has actually backed off — NOT on
    each interspersed success during a flood. (Resetting on any success made the
    disconnect threshold unreachable: backpressure refills a token or two between
    denials, so over_streak never climbed — Phase-70 review.) The clock is
    injectable so the reset/accumulate logic is deterministically testable."""

    rate: float
    burst: float
    clock: "Callable[[], float]" = time.monotonic
    tokens: float = 0.0
    last: float = 0.0
    over_streak: int = 0

    def __post_init__(self) -> None:
        self.tokens = self.burst
        self.last = self.clock()

    def allow(self) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= self.burst:  # fully refilled → the client has backed off
            self.over_streak = 0
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        self.over_streak += 1
        return False


async def _audit_ws(app: FastAPI, event: str, principal: Principal, conn_id: str) -> None:
    """Best-effort WS abuse audit via the injected composition-root hook — never
    fails the socket handler (a missing hook or a DB blip is swallowed)."""
    hook = getattr(app.state, "ws_audit", None)
    if hook is None:
        return
    try:
        await hook(event=event, user_id=principal.sub, detail={"conn_id": conn_id})
    except Exception:  # pragma: no cover - audit is never a request dependency
        logger.warning("ws.audit_failed", conn_id=conn_id)


class WebSocketAuthGuard:
    """Handshake authenticator — the WS analogue of the REST ``Requires`` guard
    (so the route-guard lint sees ``/ws`` as protected, not public). Validates the
    ``?token=`` JWT with the shared identity machinery and rejects the handshake
    on any failure."""

    __titan_requires__ = True

    async def __call__(self, websocket: WebSocket, token: str = Query(default="")) -> Principal:
        try:
            return await verify_bearer(
                websocket.app.state.redis, websocket.app.state.keyring, token
            )
        except (AuthenticationFailed, TokenInvalid) as exc:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION, reason="authentication failed"
            ) from exc


@dataclass
class _SessionState:
    """Mutable per-connection state shared by the receive + heartbeat loops."""

    token: str
    outstanding_pings: int = 0


async def serve_connection(websocket: WebSocket, principal: Principal, token: str) -> None:
    """Full lifecycle for one authenticated socket: accept, cap-check, register,
    run the frame loops, then always release the slot + unregister."""
    app: FastAPI = websocket.app
    manager: ConnectionManager = app.state.ws_manager
    registry = ConnectionRegistry(
        app.state.redis, cap=app.state.settings.ws_max_connections_per_user
    )
    buffer = ProjectBuffer(app.state.redis)
    resolver = PermissionResolver(app.state.engine, principal)
    org: str = app.state.ws_org
    user_id = str(principal.sub)

    await websocket.accept()
    conn = Connection(websocket, user_id=user_id, conn_id=str(uuid.uuid4()))

    if not await registry.acquire(user_id, conn.conn_id, time.time()):
        # Vol 3 §12 / SEC-081: at most `ws_max_connections_per_user` concurrent
        # connections per user (connect-time), audited as an abuse signal.
        cap = app.state.settings.ws_max_connections_per_user
        conn.enqueue(
            protocol.encode(
                protocol.error_frame("connection_cap", f"connection limit reached ({cap} per user)")
            )
        )
        await _audit_ws(app, WS_CONNECTION_REJECTED, principal, conn.conn_id)
        await conn.close(code=status.WS_1008_POLICY_VIOLATION, reason="connection cap exceeded")
        return

    manager.register(conn)
    conn.start_writer()
    logger.info("ws.connected", user_id=user_id, conn_id=conn.conn_id)
    try:
        await _run_loops(app, conn, websocket, principal, manager, buffer, resolver, org, token)
    finally:
        manager.unregister(conn)
        await registry.release(user_id, conn.conn_id)
        await conn.close()
        logger.info("ws.disconnected", user_id=user_id, conn_id=conn.conn_id)


async def _run_loops(
    app: FastAPI,
    conn: Connection,
    websocket: WebSocket,
    principal: Principal,
    manager: ConnectionManager,
    buffer: ProjectBuffer,
    resolver: PermissionResolver,
    org: str,
    token: str,
) -> None:
    state = _SessionState(token=token)
    stop = asyncio.Event()
    receive = asyncio.create_task(
        _receive_loop(app, conn, websocket, principal, manager, buffer, resolver, org, state, stop)
    )
    heartbeat = asyncio.create_task(_heartbeat_loop(app, conn, principal, state, stop))
    try:
        await asyncio.wait({receive, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        for task in (receive, heartbeat):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(receive, heartbeat, return_exceptions=True)


async def _receive_loop(
    app: FastAPI,
    conn: Connection,
    websocket: WebSocket,
    principal: Principal,
    manager: ConnectionManager,
    buffer: ProjectBuffer,
    resolver: PermissionResolver,
    org: str,
    state: _SessionState,
    stop: asyncio.Event,
) -> None:
    settings = app.state.settings
    gate = _MessageRateGate(
        rate=settings.ws_max_messages_per_second, burst=settings.ws_message_burst
    )
    try:
        while not stop.is_set():
            raw = await websocket.receive_text()
            if not gate.allow():
                # Sustained flood (the client ignored the backpressure) → close +
                # audit. A brief burst is absorbed by the bucket; only a run of
                # over-limit frames longer than the burst trips the disconnect.
                if gate.over_streak > settings.ws_message_burst:
                    await _audit_ws(app, WS_FLOOD_DISCONNECTED, principal, conn.conn_id)
                    logger.warning(
                        "ws.flood_disconnect", user_id=conn.user_id, conn_id=conn.conn_id
                    )
                    await conn.close(
                        code=status.WS_1008_POLICY_VIOLATION, reason="message rate exceeded"
                    )
                    break
                await asyncio.sleep(_BACKPRESSURE_SLEEP)  # backpressure the flooder
                continue
            try:
                frame = protocol.parse_client_frame(raw)
            except protocol.ProtocolError as exc:
                conn.enqueue(protocol.encode(protocol.error_frame(exc.code, exc.message)))
                continue
            await _handle_frame(app, frame, conn, principal, manager, buffer, resolver, org, state)
    except (WebSocketDisconnect, RuntimeError):
        pass  # client hung up (or the socket was closed by the heartbeat loop)
    finally:
        stop.set()


async def _heartbeat_loop(
    app: FastAPI,
    conn: Connection,
    principal: Principal,
    state: _SessionState,
    stop: asyncio.Event,
) -> None:
    # Refresh THIS connection's cap lease every tick. Without it the 90 s lease
    # lapses on a still-live socket, so it stops counting toward the per-user cap
    # and an attacker can open unbounded connections by cycling past each 90 s
    # window (Phase-70 review, CRITICAL — registry.refresh was dead code). The
    # 25 s tick is well under the 90 s lease.
    registry = ConnectionRegistry(
        app.state.redis, cap=app.state.settings.ws_max_connections_per_user
    )
    try:
        while not stop.is_set():
            if await _wait_or_stopped(stop, HEARTBEAT_SECONDS):
                return
            await registry.refresh(conn.user_id, conn.conn_id, time.time())
            # TITAN-SEC-016: expiry/revocation closes the socket in ≤ 60 s (checked
            # every 25 s) unless the client re-authenticated in-band.
            if await _token_dead(app, state.token):
                conn.enqueue(
                    protocol.encode(
                        protocol.error_frame("token_expired", "token expired or revoked")
                    )
                )
                await conn.close(
                    code=status.WS_1008_POLICY_VIOLATION, reason="token expired or revoked"
                )
                return
            if state.outstanding_pings >= MAX_MISSED_PONGS:
                await conn.close(code=status.WS_1001_GOING_AWAY, reason="missed pongs")
                return
            conn.enqueue(protocol.encode(protocol.build_frame(protocol.PING, seq=0, payload={})))
            state.outstanding_pings += 1
    finally:
        stop.set()


async def _token_dead(app: FastAPI, token: str) -> bool:
    try:
        await verify_bearer(app.state.redis, app.state.keyring, token)
    except (AuthenticationFailed, TokenInvalid):
        return True
    return False


async def _handle_frame(
    app: FastAPI,
    frame: protocol.ClientFrame,
    conn: Connection,
    principal: Principal,
    manager: ConnectionManager,
    buffer: ProjectBuffer,
    resolver: PermissionResolver,
    org: str,
    state: _SessionState,
) -> None:
    if isinstance(frame, protocol.SubscribeFrame):
        await _subscribe(frame, conn, manager, buffer, resolver, org)
    elif isinstance(frame, protocol.UnsubscribeFrame):
        if frame.project_id is not None:
            manager.unsubscribe_project(conn, str(frame.project_id))
        conn.enqueue(
            protocol.encode(
                protocol.build_frame(
                    protocol.UNSUBSCRIBED,
                    seq=0,
                    payload={"project_id": str(frame.project_id) if frame.project_id else None},
                )
            )
        )
    elif isinstance(frame, protocol.PingFrame):
        conn.enqueue(protocol.encode(protocol.build_frame(protocol.PONG, seq=0, payload={})))
    elif isinstance(frame, protocol.PongFrame):
        state.outstanding_pings = 0
    elif isinstance(frame, protocol.AuthenticateFrame):
        await _reauthenticate(app, frame, conn, principal, state)


async def _subscribe(
    frame: protocol.SubscribeFrame,
    conn: Connection,
    manager: ConnectionManager,
    buffer: ProjectBuffer,
    resolver: PermissionResolver,
    org: str,
) -> None:
    # Org activity feed: joining it requires org-wide read (Organization Overview
    # is an org-level surface). Without it the feed would leak every project's
    # events to a caller who cannot read those projects (TITAN-SEC-032).
    if _has_org_topic(frame.topics, org):
        if permission_granted(SUBSCRIBE_PERMISSION, await resolver.org_permissions()):
            manager.subscribe_org(conn, org)
        else:
            conn.enqueue(
                protocol.encode(
                    protocol.error_frame("forbidden", "org activity requires org-wide read")
                )
            )

    if frame.project_id is None:
        conn.enqueue(
            protocol.encode(
                protocol.build_frame(
                    protocol.SUBSCRIBED, seq=0, payload={"project_id": None, "topics": frame.topics}
                )
            )
        )
        return

    pid = str(frame.project_id)
    if pid not in conn.requested and len(conn.requested) >= MAX_PROJECT_SUBSCRIPTIONS:
        conn.enqueue(
            protocol.encode(
                protocol.error_frame("subscription_limit", "too many project subscriptions")
            )
        )
        return
    conn.requested.add(pid)

    granted = await resolver.project_permissions(frame.project_id)
    if not granted:
        # Object-scope: no visibility into the project → 404-equivalent (existence
        # never leaked, TITAN-SEC-032).
        conn.enqueue(
            protocol.encode(protocol.error_frame("not_found", "no such project", project_id=pid))
        )
        return
    if not permission_granted(SUBSCRIBE_PERMISSION, granted):
        conn.enqueue(
            protocol.encode(
                protocol.error_frame("forbidden", "missing project:read", project_id=pid)
            )
        )
        return

    if frame.last_seq is None:
        manager.subscribe_project(conn, pid, frame.topics)
        conn.enqueue(_subscribed_ack(pid, frame.topics))
        return

    # Reconnect with a cursor: register FIRST (under a hold) so no live frame is
    # missed, replay the buffered gap in order, then flush the held live frames.
    conn.begin_hold(pid)
    manager.subscribe_project(conn, pid, frame.topics)
    replayed = await buffer.replay(pid, frame.last_seq)
    if replayed is RESYNC:
        conn.enqueue(
            protocol.encode(
                protocol.build_frame(protocol.SYS_RESYNC, seq=0, payload={}, project_id=pid)
            )
        )
    else:
        for buffered in replayed:  # type: ignore[union-attr]
            conn.enqueue(protocol.encode(buffered))
    conn.enqueue(_subscribed_ack(pid, frame.topics))
    conn.end_hold(pid)  # flush frames published during replay, in seq order


def _subscribed_ack(pid: str, topics: list[str]) -> str:
    return protocol.encode(
        protocol.build_frame(
            protocol.SUBSCRIBED,
            seq=0,
            payload={"project_id": pid, "topics": topics},
            project_id=pid,
        )
    )


async def _reauthenticate(
    app: FastAPI,
    frame: protocol.AuthenticateFrame,
    conn: Connection,
    principal: Principal,
    state: _SessionState,
) -> None:
    try:
        renewed = await verify_bearer(app.state.redis, app.state.keyring, frame.token)
    except (AuthenticationFailed, TokenInvalid):
        conn.enqueue(protocol.encode(protocol.error_frame("auth_failed", "invalid token")))
        return
    if renewed.sub != principal.sub:
        # A socket may refresh its own token, never swap to another identity.
        conn.enqueue(protocol.encode(protocol.error_frame("auth_failed", "identity mismatch")))
        return
    state.token = frame.token
    conn.enqueue(protocol.encode(protocol.build_frame(protocol.AUTHENTICATED, seq=0, payload={})))


def _has_org_topic(topics: list[str], org: str) -> bool:
    wanted = {protocol.TOPIC_ORG_ACTIVITY, f"org:{org}:activity"}
    return any(t in wanted for t in topics)


async def _wait_or_stopped(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep ``seconds`` or until ``stop`` is set; returns True if it was set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    return stop.is_set()


__all__ = ["WebSocketAuthGuard", "serve_connection"]
