"""The agent message bus: persistence-before-enqueue, per-agent inbox delivery,
idempotency, the requires_ack watchdog seam, chaos replay, and the DecisionLog
append-only DB policy (Vol 2 §6.3/§7, TITAN-AGT-016/019). Needs the disposable
test DB + fakeredis."""

import uuid
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.events.outbox import OutboxDrainer
from titan.events.redis_bus import RedisStreamBus
from titan.messaging import service as messaging
from titan.messaging.bus import RUNTIME_GROUP, clear_watchdogs, inbox_stream
from titan.messaging.protocol import AgentMessage

pytestmark = pytest.mark.schema


def _bus() -> tuple[RedisStreamBus, Any]:
    return RedisStreamBus(fakeredis.aioredis.FakeRedis(decode_responses=True)), None


async def _make_project(engine: Any) -> uuid.UUID:
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'creator', 'local', :s)"
            ),
            {"id": uid, "e": f"u-{uid.hex[:12]}@t.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :n, :d, 'b', :u)"
            ),
            {"id": pid, "n": f"p-{pid.hex[:12]}", "d": "P", "u": uid},
        )
    return pid


def _msg(pid: uuid.UUID, *, to: str = "eng_backend", requires_ack: bool = False) -> AgentMessage:
    return AgentMessage(
        type="TaskAssignment",
        project_id=pid,
        from_agent="pm_project",
        to_agent=to,
        requires_ack=requires_ack,
        payload={"title": "do", "description": "the thing", "acceptance_criteria": ["x"]},
    )


def test_send_persists_before_enqueue(migrated_engine: Any) -> None:
    """A sent message is in Postgres (messages + outbox) BEFORE it reaches Redis
    — nothing an agent sent lives only in the queue (TITAN-AGT-016)."""

    async def scenario() -> None:
        bus, _ = _bus()
        pid = await _make_project(migrated_engine)
        msg = _msg(pid)
        async with migrated_engine.begin() as conn:
            await messaging.send_agent_message(conn, msg, actor="pm_project")

        stream = inbox_stream(pid, "eng_backend")
        async with migrated_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT kind, from_agent, to_agent, priority, requires_ack, correlation_id "
                        "FROM messages WHERE id = :id"
                    ),
                    {"id": msg.message_id},
                )
            ).one()
            assert row.kind == "task_assignment"
            assert row.from_agent == "pm_project" and row.to_agent == "eng_backend"
            assert row.correlation_id == msg.correlation_id
            unpub = await conn.execute(
                text("SELECT count(*) FROM outbox WHERE id = :id AND published_at IS NULL"),
                {"id": msg.message_id},
            )
            assert unpub.scalar() == 1  # outbox row id IS the message_id
        assert await bus._redis.xlen(stream) == 0  # not yet on the stream

        assert await OutboxDrainer(migrated_engine, bus).drain_once() >= 1
        assert await bus._redis.xlen(stream) == 1  # now delivered to the inbox

    run_async(scenario)


def test_delivery_is_idempotent_on_message_id(migrated_engine: Any) -> None:
    async def scenario() -> None:
        bus, _ = _bus()
        pid = await _make_project(migrated_engine)
        msg = _msg(pid)
        async with migrated_engine.begin() as conn:
            await messaging.send_agent_message(conn, msg, actor="pm_project")
        drainer = OutboxDrainer(migrated_engine, bus)
        await drainer.drain_once()
        assert await drainer.drain_once() == 0  # published_at stamped → no re-publish

        stream = inbox_stream(pid, "eng_backend")
        await bus.ensure_group(stream, RUNTIME_GROUP)
        first = await bus.consume(stream, RUNTIME_GROUP, "c1", block_ms=100)
        assert len(first) == 1
        assert first[0].envelope.event_id == str(msg.message_id)  # dedup key = message_id
        await bus.ack(stream, RUNTIME_GROUP, first[0])
        # A re-published duplicate (same event_id) is dropped on consume.
        await bus.publish(
            stream, msg.model_dump(mode="json"), actor="x", event_id=str(msg.message_id)
        )
        assert await bus.consume(stream, RUNTIME_GROUP, "c1", block_ms=100) == []

    run_async(scenario)


def test_consume_inbox_and_ack(migrated_engine: Any) -> None:
    async def scenario() -> None:
        bus, _ = _bus()
        pid = await _make_project(migrated_engine)
        msg = _msg(pid)
        async with migrated_engine.begin() as conn:
            await messaging.send_agent_message(conn, msg, actor="pm_project")
        await OutboxDrainer(migrated_engine, bus).drain_once()

        got = await messaging.consume_inbox(bus, pid, "eng_backend", "runtime-1", block_ms=100)
        assert len(got) == 1
        delivered = AgentMessage.model_validate(got[0].envelope.payload)
        assert delivered.message_id == msg.message_id
        assert delivered.typed_payload().model_dump()["title"] == "do"
        await messaging.ack_inbox(bus, pid, "eng_backend", got[0])
        # Nothing left after ack.
        assert (
            await messaging.consume_inbox(bus, pid, "eng_backend", "runtime-1", block_ms=100) == []
        )

    run_async(scenario)


def test_requires_ack_unacked_reenters_pending_and_fires_watchdog(migrated_engine: Any) -> None:
    """A requires_ack message left unacked past its timeout is reclaimed AND
    fires the watchdog hook (the Phase-32 escalation seam)."""

    async def scenario() -> None:
        clear_watchdogs()
        fired: list[str] = []

        async def hook(message: AgentMessage) -> None:
            fired.append(str(message.message_id))

        messaging.register_watchdog(hook)
        try:
            bus, _ = _bus()
            pid = await _make_project(migrated_engine)
            msg = _msg(pid, requires_ack=True)
            async with migrated_engine.begin() as conn:
                await messaging.send_agent_message(conn, msg, actor="pm_project")
            await OutboxDrainer(migrated_engine, bus).drain_once()

            # A runtime reads it but "dies" without acking → left pending.
            got = await messaging.consume_inbox(bus, pid, "eng_backend", "dead", block_ms=100)
            assert len(got) == 1

            # Past its timeout (min_idle=0 forces it), the reaper reclaims it and
            # the watchdog fires.
            reclaimed = await messaging.reclaim_unacked(
                bus, pid, "eng_backend", "watchdog", min_idle_ms=0
            )
            assert len(reclaimed) == 1
            assert fired == [str(msg.message_id)]
        finally:
            clear_watchdogs()

    run_async(scenario)


def test_no_watchdog_for_non_ack_messages(migrated_engine: Any) -> None:
    async def scenario() -> None:
        clear_watchdogs()
        fired: list[str] = []
        messaging.register_watchdog(lambda m: _record(fired, m))
        try:
            bus, _ = _bus()
            pid = await _make_project(migrated_engine)
            msg = _msg(pid, requires_ack=False)
            async with migrated_engine.begin() as conn:
                await messaging.send_agent_message(conn, msg, actor="pm_project")
            await OutboxDrainer(migrated_engine, bus).drain_once()
            await messaging.consume_inbox(bus, pid, "eng_backend", "dead", block_ms=100)
            reclaimed = await messaging.reclaim_unacked(
                bus, pid, "eng_backend", "watchdog", min_idle_ms=0
            )
            assert len(reclaimed) == 1  # still reclaimed (at-least-once)…
            assert fired == []  # …but no watchdog for a non-ack message
        finally:
            clear_watchdogs()

    run_async(scenario)


async def _record(sink: list[str], message: AgentMessage) -> None:
    sink.append(str(message.message_id))


def test_chaos_redis_loss_leaves_no_missing_messages(migrated_engine: Any) -> None:
    """Kill Redis mid-conversation: every accepted message is still in Postgres,
    and a fresh Redis re-drains the not-yet-delivered ones — zero loss
    (TITAN-AGT-016 acceptance)."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        sent: list[uuid.UUID] = []
        async with migrated_engine.begin() as conn:
            for i in range(3):
                msg = _msg(pid, to=f"eng_{i}")
                await messaging.send_agent_message(conn, msg, actor="pm_project")
                sent.append(msg.message_id)

        # Redis #1 receives only the first drained message, then "dies".
        bus1, _ = _bus()
        # Drain exactly one row, then abandon this Redis.
        async with migrated_engine.begin() as conn:
            first = (
                await conn.execute(
                    text(
                        "SELECT id, topic, payload FROM outbox WHERE published_at IS NULL "
                        "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
                    )
                )
            ).one()
            import json

            wrapper = json.loads(first.payload) if isinstance(first.payload, str) else first.payload
            await bus1.publish(first.topic, wrapper["data"], actor="x", event_id=str(first.id))
            await conn.execute(
                text("UPDATE outbox SET published_at = now() WHERE id = :id"), {"id": first.id}
            )

        # Postgres is the source of truth: all 3 persisted regardless of Redis.
        async with migrated_engine.connect() as conn:
            persisted = await messaging.messages_for_project(conn, pid)
        assert {m.id for m in persisted} == set(sent)

        # A brand-new Redis (the "recovery") re-drains the 2 undelivered rows,
        # so no accepted message was lost to the dead Redis.
        bus2, _ = _bus()
        recovered = await OutboxDrainer(migrated_engine, bus2).drain_once()
        assert recovered == 2
        total_on_new_redis = 0
        for i in range(3):
            total_on_new_redis += await bus2._redis.xlen(inbox_stream(pid, f"eng_{i}"))
        assert total_on_new_redis == 2  # the 2 that hadn't been delivered pre-crash

    run_async(scenario)


def test_send_span_carries_no_payload_content(migrated_engine: Any) -> None:
    """The per-message OTel span carries envelope metadata only — never payload
    text (TITAN-OPS-016: no message/prompt content in traces)."""

    async def scenario() -> None:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from titan.messaging import bus as bus_mod

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        # Point the bus's tracer at our in-memory provider for this test.
        original = bus_mod._TRACER
        bus_mod._TRACER = provider.get_tracer("test")
        try:
            bus, _ = _bus()
            pid = await _make_project(migrated_engine)
            secret = "TOP-SECRET-DISCOUNT-LOGIC-should-not-appear-in-traces"
            msg = AgentMessage(
                type="TaskAssignment",
                project_id=pid,
                from_agent="pm_project",
                to_agent="eng_backend",
                payload={"title": secret, "description": secret, "acceptance_criteria": [secret]},
            )
            async with migrated_engine.begin() as conn:
                await messaging.send_agent_message(conn, msg, actor="pm_project")
        finally:
            bus_mod._TRACER = original

        spans = exporter.get_finished_spans()
        assert len(spans) == 1 and spans[0].name == "agent.message"
        blob = repr(dict(spans[0].attributes or {}))
        assert secret not in blob  # no payload content leaked into span attributes
        # …but the useful metadata IS there.
        assert spans[0].attributes is not None
        assert spans[0].attributes["messaging.type"] == "TaskAssignment"
        assert str(msg.correlation_id) in blob

    run_async(scenario)


def test_decisions_are_db_append_only(migrated_engine: Any) -> None:
    """UPDATE and DELETE on a decision row are rejected at the DB layer, not just
    in app code (TITAN-AGT-019 acceptance)."""

    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        did = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO decisions (id, project_id, title, context, decision, "
                    "alternatives, consequences, status) VALUES "
                    "(:id, :p, 't', 'c', 'd', 'a', 'q', 'accepted')"
                ),
                {"id": did, "p": pid},
            )

        from sqlalchemy.exc import DBAPIError

        with pytest.raises(DBAPIError):
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text("UPDATE decisions SET title = 'hacked' WHERE id = :id"), {"id": did}
                )
        with pytest.raises(DBAPIError):
            async with migrated_engine.begin() as conn:
                await conn.execute(text("DELETE FROM decisions WHERE id = :id"), {"id": did})

        # The row is untouched.
        async with migrated_engine.connect() as conn:
            title = await conn.execute(
                text("SELECT title FROM decisions WHERE id = :id"), {"id": did}
            )
            assert title.scalar_one() == "t"

    run_async(scenario)
