"""End-to-end WS wire tests over the real FastAPI ``/ws`` route (Phase 24),
proving the handshake guard, subscribe RBAC, ping/pong and protocol errors on the
genuine transport. Uses the shared ``api`` fixture (real Postgres schema + seeded
§9.3 roles + fakeredis), so it is ``schema``-marked. Fan-out *delivery* is proven
hermetically in test_realtime_dispatch.py (TestClient cannot receive a pub/sub
frame published from another event loop); here we assert the request/reply wire."""

import contextlib
import uuid
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.schema


def _token(api: dict[str, Any], role: str) -> str:
    header: str = api["token_for"](role)["Authorization"]
    return header.removeprefix("Bearer ")


def test_handshake_rejects_bad_and_missing_token(api: dict[str, Any]) -> None:
    client = api["client"]
    for url in ("/ws?token=garbage", "/ws"):
        with contextlib.suppress(WebSocketDisconnect), client.websocket_connect(url) as ws:
            ws.receive_json()
            raise AssertionError(f"{url} should have been rejected at the handshake")


def test_connect_subscribe_ack_and_ping_pong(api: dict[str, Any]) -> None:
    client = api["client"]
    pid = str(uuid.uuid4())
    with client.websocket_connect(f"/ws?token={_token(api, 'viewer')}") as ws:
        ws.send_json({"type": "subscribe", "project_id": pid, "topics": ["tasks"]})
        ack = ws.receive_json()
        assert ack["type"] == "subscribed" and ack["payload"]["project_id"] == pid
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        ws.send_json({"type": "unsubscribe", "project_id": pid})
        assert ws.receive_json()["type"] == "unsubscribed"


def test_unauthorized_subscribe_is_typed_error(api: dict[str, Any]) -> None:
    client = api["client"]
    # The 'scoped' user has no org-wide role → no visibility into a project.
    with client.websocket_connect(f"/ws?token={_token(api, 'scoped')}") as ws:
        ws.send_json({"type": "subscribe", "project_id": str(uuid.uuid4())})
        err = ws.receive_json()
        assert err["type"] == "error" and err["payload"]["code"] == "not_found"


def test_protocol_error_frame_keeps_socket_open(api: dict[str, Any]) -> None:
    client = api["client"]
    with client.websocket_connect(f"/ws?token={_token(api, 'viewer')}") as ws:
        ws.send_text("}{ not json")
        err = ws.receive_json()
        assert err["type"] == "error" and err["payload"]["code"] == "bad_frame"
        # Still usable afterwards.
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_message_rate_gate_bursts_then_throttles() -> None:
    """SEC-081 unit: the inbound token bucket admits a burst, then denies and
    counts the over-limit run (the disconnect trigger)."""
    from titan.realtime.endpoint import _MessageRateGate

    gate = _MessageRateGate(rate=1.0, burst=3.0, clock=lambda: 0.0)  # frozen clock
    assert [gate.allow() for _ in range(3)] == [True, True, True]  # burst absorbed
    denied = [gate.allow() for _ in range(3)]
    assert denied == [False, False, False]  # no tokens left this instant
    assert gate.over_streak == 3  # the sustained-violation counter climbs


def test_gate_violation_streak_survives_interspersed_allows() -> None:
    """SEC-081 re-break (Phase-70 review): during a flood, backpressure refills a
    token or two between denials — over_streak must NOT reset on those
    interspersed allows, or the disconnect threshold is never reached. It resets
    ONLY when the bucket fully refills (the client actually backed off)."""
    from titan.realtime.endpoint import _MessageRateGate

    now = {"t": 0.0}
    gate = _MessageRateGate(rate=10.0, burst=5.0, clock=lambda: now["t"])
    for _ in range(5):  # spend the burst
        assert gate.allow()
    # Simulate a sustained flood: advance 0.05s each frame (refills 0.5 token) so
    # allows and denials alternate. over_streak must keep climbing, not reset.
    denials = 0
    for _ in range(60):
        now["t"] += 0.05
        if not gate.allow():
            denials += 1
    assert denials > 20
    assert gate.over_streak > 20  # climbs past a disconnect threshold despite allows

    # Now the client backs off: a long quiet period fully refills the bucket.
    now["t"] += 10.0
    assert gate.allow()
    assert gate.over_streak == 0  # reset only on a fully-refilled (idle) bucket


def test_ws_flood_triggers_disconnect(api: dict[str, Any]) -> None:
    """SEC-081 integration: a client that floods far past its burst is
    disconnected (the flood-test acceptance)."""
    client = api["client"]
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"/ws?token={_token(api, 'viewer')}") as ws,
    ):
        # Enough frames that the violation streak climbs past the burst(40)
        # disconnect threshold despite backpressure-interspersed allows.
        for _ in range(260):
            ws.send_json({"type": "ping"})
        for _ in range(120):
            ws.receive_json()


def test_connection_cap_rejects_the_sixth(api: dict[str, Any]) -> None:
    client = api["client"]
    token = _token(api, "viewer")
    with contextlib.ExitStack() as stack:
        for _ in range(5):
            stack.enter_context(client.websocket_connect(f"/ws?token={token}"))
        # The 6th connection for the same user is refused at connect.
        with contextlib.suppress(WebSocketDisconnect):
            ws = stack.enter_context(client.websocket_connect(f"/ws?token={token}"))
            frame = ws.receive_json()
            assert frame["type"] == "error" and frame["payload"]["code"] == "connection_cap"
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
