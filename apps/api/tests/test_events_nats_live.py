"""QueueBus conformance for the NATS JetStream backend (Phase 81, ADR-005, FND-051).

The SAME scenarios as test_events_bus.py (dedup, reclaim, poison→DLQ, pending depth,
provided-id) — proving the config-only swap satisfies the identical contract — but run
against a REAL nats-server (no in-memory JetStream fake exists), so this is an opt-in
live test like test_llm_*_live.py: it SKIPS when no server is reachable and runs in CI
(a nats-server service) or locally with `nats-server -js` + TITAN_TEST_NATS_URL.

Each test uses a UNIQUE topic AND group → its own stream/durable/DLQ/seen-KV bucket, so
runs are isolated from each other and from a persistent nats-server store (no teardown).
"""

from __future__ import annotations

import hashlib
import os
import socket
from typing import Any
from urllib.parse import urlparse

import pytest
import uuid_utils

from tests.conftest import run_async
from titan.events.bus import InvalidEnvelope
from titan.events.nats_bus import NatsStreamBus, stream_name_for
from titan.events.streams import dlq_stream

NATS_URL = os.environ.get("TITAN_TEST_NATS_URL", "nats://127.0.0.1:4222")


def _nats_reachable() -> bool:
    """One cheap TCP probe at collection, so the whole module SKIPS fast when no server
    is up (else every test would burn a 2 s connect timeout in the normal unit suite)."""
    u = urlparse(NATS_URL)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 4222), timeout=0.5):
            return True
    except OSError:
        return False


pytestmark: list[Any] = (
    [] if _nats_reachable() else [pytest.mark.skip(reason=f"no nats-server at {NATS_URL}")]
)


class _HmacSigner:
    """A minimal Signer (structurally satisfies the protocol) keyed by ``key``."""

    def __init__(self, key: str) -> None:
        self._key = key

    def sign(self, payload: str) -> str:
        return hashlib.sha256(f"{self._key}:{payload}".encode()).hexdigest()

    def verify(self, payload: str, signature: str) -> bool:
        return signature == self.sign(payload)


async def _connect() -> Any:
    """Connect, or skip the test if no nats-server is reachable."""
    import nats
    from nats.errors import NoServersError

    try:
        return await nats.connect(NATS_URL, connect_timeout=2, allow_reconnect=False)
    except (NoServersError, OSError, TimeoutError, Exception) as exc:  # noqa: BLE001
        pytest.skip(f"no nats-server at {NATS_URL}: {exc}")


def _ids() -> tuple[str, str]:
    """A unique (topic, group) so each test gets fresh stream/durable/seen-KV state."""
    u = str(uuid_utils.uuid7())
    return f"stream:test.{u}", f"g-{u}"


def test_nats_publish_consume_ack_round_trip() -> None:
    async def scenario() -> None:
        nc = await _connect()
        try:
            bus = NatsStreamBus(nc.jetstream())
            topic, group = _ids()
            await bus.ensure_group(topic, group)
            eid = await bus.publish(topic, {"n": 1}, actor="api", trace_id="t1")

            got = await bus.consume(topic, group, "c1", block_ms=2000)
            assert len(got) == 1
            assert got[0].envelope.event_id == eid
            assert got[0].envelope.payload == {"n": 1}
            assert got[0].envelope.actor == "api" and got[0].envelope.trace_id == "t1"

            await bus.ack(topic, group, got[0])
            assert await bus.consume(topic, group, "c1", block_ms=300) == []
        finally:
            await nc.close()

    run_async(scenario)


def test_nats_provided_event_id_is_preserved() -> None:
    async def scenario() -> None:
        nc = await _connect()
        try:
            bus = NatsStreamBus(nc.jetstream())
            topic, group = _ids()
            await bus.ensure_group(topic, group)
            eid = await bus.publish(topic, {}, actor="sys", event_id="fixed-1")
            assert eid == "fixed-1"
            got = await bus.consume(topic, group, "c1", block_ms=2000)
            assert len(got) == 1 and got[0].envelope.event_id == "fixed-1"
        finally:
            await nc.close()

    run_async(scenario)


def test_nats_duplicate_delivery_is_a_no_op() -> None:
    async def scenario() -> None:
        nc = await _connect()
        try:
            bus = NatsStreamBus(nc.jetstream())
            topic, group = _ids()
            await bus.ensure_group(topic, group)
            await bus.publish(topic, {"n": 1}, actor="api", event_id="dup-1")
            first = await bus.consume(topic, group, "c1", block_ms=2000)
            assert len(first) == 1
            await bus.ack(topic, group, first[0])

            await bus.publish(topic, {"n": 1}, actor="api", event_id="dup-1")
            second = await bus.consume(topic, group, "c1", block_ms=1000)
            assert second == []  # deduped on the seen ledger — no second delivery
        finally:
            await nc.close()

    run_async(scenario)


def test_nats_killed_consumer_pending_is_reclaimed() -> None:
    async def scenario() -> None:
        nc = await _connect()
        try:
            bus = NatsStreamBus(nc.jetstream())
            topic, group = _ids()
            await bus.ensure_group(topic, group)
            await bus.publish(topic, {"n": 1}, actor="api", event_id="orphan-1")

            got = await bus.consume(topic, group, "c1", block_ms=2000)
            assert len(got) == 1
            await bus.nack(topic, group, got[0])  # c1 "dies" without acking

            reclaimed = await bus.reap(topic, group, "c2", min_idle_ms=0)
            assert len(reclaimed) == 1
            assert reclaimed[0].envelope.event_id == "orphan-1"
            assert reclaimed[0].delivery_count >= 2  # redelivered
        finally:
            await nc.close()

    run_async(scenario)


def test_nats_pending_depth_reports_unacked() -> None:
    async def scenario() -> None:
        nc = await _connect()
        try:
            bus = NatsStreamBus(nc.jetstream())
            topic, group = _ids()
            await bus.ensure_group(topic, group)
            await bus.publish(topic, {}, actor="api", event_id="p1")
            await bus.publish(topic, {}, actor="api", event_id="p2")
            assert await bus.pending_depth(topic, group) == 0  # nothing delivered yet
            got = await bus.consume(topic, group, "c1", count=10, block_ms=2000)
            assert len(got) == 2
            assert await bus.pending_depth(topic, group) == 2  # both delivered, unacked
            await bus.ack(topic, group, got[0])
            assert await bus.pending_depth(topic, group) == 1
        finally:
            await nc.close()

    run_async(scenario)


def test_nats_signed_envelope_verifies_and_a_forgery_is_dropped() -> None:
    """TITAN-SEC-020 parity on JetStream: a correctly-signed envelope round-trips;
    one signed with the WRONG key is DROPPED (acked, not delivered) and audited."""

    async def scenario() -> None:
        nc = await _connect()
        try:
            topic, group = _ids()
            rejected: list[InvalidEnvelope] = []

            async def _on_invalid(inv: InvalidEnvelope) -> None:
                rejected.append(inv)

            good = NatsStreamBus(nc.jetstream(), signer=_HmacSigner("k1"), on_invalid=_on_invalid)
            await good.ensure_group(topic, group)
            await good.publish(topic, {"n": 1}, actor="api", event_id="signed-1")
            got = await good.consume(topic, group, "c1", block_ms=2000)
            assert len(got) == 1 and got[0].envelope.event_id == "signed-1"
            await good.ack(topic, group, got[0])

            # A frame signed with the wrong key: the verify side (k1) must reject it.
            forger = NatsStreamBus(nc.jetstream(), signer=_HmacSigner("attacker"))
            await forger.publish(topic, {"n": 2}, actor="api", event_id="forged-1")
            out = await good.consume(topic, group, "c1", block_ms=1000)
            assert out == []  # dropped, never delivered to a handler
            assert any(r.reason == "bad_signature" and r.event_id == "forged-1" for r in rejected)
        finally:
            await nc.close()

    run_async(scenario)


def test_nats_poison_message_goes_to_dlq_after_three_attempts() -> None:
    async def scenario() -> None:
        nc = await _connect()
        try:
            js = nc.jetstream()
            bus = NatsStreamBus(js)
            topic, group = _ids()
            await bus.ensure_group(topic, group)
            await bus.publish(topic, {"bad": True}, actor="api", event_id="poison-1")

            d1 = await bus.consume(topic, group, "c1", block_ms=2000)
            assert len(d1) == 1 and d1[0].delivery_count == 1
            await bus.nack(topic, group, d1[0])

            d2 = await bus.reap(topic, group, "c2", min_idle_ms=0, max_attempts=3)
            assert len(d2) == 1 and d2[0].delivery_count == 2
            await bus.nack(topic, group, d2[0])

            d3 = await bus.reap(topic, group, "c3", min_idle_ms=0, max_attempts=3)
            assert len(d3) == 1 and d3[0].delivery_count == 3
            await bus.nack(topic, group, d3[0])

            d4 = await bus.reap(topic, group, "c4", min_idle_ms=0, max_attempts=3)
            assert d4 == []  # over max_attempts → dead-lettered, not returned
            info = await js.stream_info(stream_name_for(dlq_stream(topic)))
            assert info.state.messages == 1
        finally:
            await nc.close()

    run_async(scenario)
