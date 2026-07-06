"""Hermetic tests for the WS fan-out consumer (Phase 24): every domain event
type from the Vol 3 §5 table maps to the right pub/sub channel with the fixed
envelope, project frames are buffered (deltas are not), and the whole
outbox-stream → channels path works end-to-end over fakeredis + the real
RedisStreamBus. Single asyncio loop; no Postgres, no real Redis."""

import datetime as dt
import json
import uuid
from typing import Any

import fakeredis.aioredis

from titan.events.envelope import Envelope
from titan.events.redis_bus import RedisStreamBus
from titan.events.streams import STREAM_EVENTS_DOMAIN
from titan.realtime import channels
from titan.realtime.fanout import WS_FANOUT_GROUP, WsFanout, process_ws_fanout_batch

from .conftest import run_async

_PID = str(uuid.uuid4())
_UID = str(uuid.uuid4())


def _fake() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _env(payload: dict[str, Any]) -> Envelope:
    return Envelope(
        event_id=str(uuid.uuid4()),
        topic=STREAM_EVENTS_DOMAIN,
        occurred_at=dt.datetime(2026, 7, 6, 12, 0, tzinfo=dt.UTC),
        actor="test",
        payload=payload,
    )


async def _collect(fanout: WsFanout, redis: Any, payload: dict[str, Any]) -> list[tuple[str, Any]]:
    ps = redis.pubsub()
    await ps.psubscribe("ws.project.*", "notifications:*", "org:*:activity")
    await ps.subscribe("agents:status")
    for _ in range(4):
        await ps.get_message(timeout=0.05)
    await fanout.handle_envelope(_env(payload))
    out: list[tuple[str, Any]] = []
    while True:
        message = await ps.get_message(timeout=0.1)
        if message is None:
            break
        if message["type"] in ("message", "pmessage"):
            out.append((message["channel"], json.loads(message["data"])))
    await ps.aclose()
    return out


def test_task_event_fans_to_project_and_org() -> None:
    async def scenario() -> None:
        redis = _fake()
        fanout = WsFanout(redis, org="titan")
        task = {"id": str(uuid.uuid4()), "project_id": _PID, "title": "T"}
        published = await _collect(fanout, redis, {"type": "task.updated", "task": task})
        by_channel = dict(published)
        # Project channel carries the task DTO in the fixed envelope.
        proj = by_channel[channels.project_channel(_PID)]
        assert proj["type"] == "task.updated" and proj["payload"] == task
        assert proj["project_id"] == _PID and proj["seq"] == 1 and proj["ts"].endswith("Z")
        # Mirrored to the org activity feed as activity.created.
        org = by_channel[channels.org_channel("titan")]
        assert org["type"] == "activity.created" and org["payload"]["event"] == "task.updated"
        # Buffered for replay.
        assert await redis.llen(channels.buffer_key(_PID)) == 1
        await redis.aclose()

    run_async(scenario)


def test_run_lifecycle_and_delta() -> None:
    async def scenario() -> None:
        redis = _fake()
        fanout = WsFanout(redis, org="titan")
        run = {"run_id": "r1", "project_id": _PID, "status": "running"}
        started = dict(await _collect(fanout, redis, {"type": "run.started", "run": run}))
        assert started[channels.project_channel(_PID)]["type"] == "run.started"
        assert await redis.llen(channels.buffer_key(_PID)) == 1  # buffered

        # run.output.delta is ephemeral: published but NOT buffered.
        delta_payload = {
            "type": "run.output.delta",
            "project_id": _PID,
            "run_id": "r1",
            "delta": "x",
        }
        delta = dict(await _collect(fanout, redis, delta_payload))
        frame = delta[channels.project_channel(_PID)]
        assert frame["type"] == "run.output.delta" and frame["payload"] == {
            "run_id": "r1",
            "delta": "x",
        }
        assert await redis.llen(channels.buffer_key(_PID)) == 1  # unchanged — not buffered
        await redis.aclose()

    run_async(scenario)


def test_message_decision_sprint_go_to_project() -> None:
    async def scenario() -> None:
        redis = _fake()
        fanout = WsFanout(redis, org="titan")
        for payload, key in (
            (
                {"type": "message.created", "message": {"project_id": _PID, "id": "m"}},
                "message.created",
            ),
            (
                {"type": "decision.recorded", "decision": {"project_id": _PID, "id": "d"}},
                "decision.recorded",
            ),
            (
                {"type": "sprint.updated", "sprint": {"project_id": _PID, "id": "s"}},
                "sprint.updated",
            ),
        ):
            published = dict(await _collect(fanout, redis, payload))
            assert published[channels.project_channel(_PID)]["type"] == key
        await redis.aclose()

    run_async(scenario)


def test_agent_status_is_global() -> None:
    async def scenario() -> None:
        redis = _fake()
        fanout = WsFanout(redis, org="titan")
        payload = {
            "type": "agent.status",
            "agent_id": "a1",
            "state": "busy",
            "current_task_id": None,
        }
        published = dict(await _collect(fanout, redis, payload))
        frame = published[channels.AGENTS_STATUS_CHANNEL]
        assert frame["type"] == "agent.status" and frame["payload"]["state"] == "busy"
        assert frame["project_id"] is None
        await redis.aclose()

    run_async(scenario)


def test_notification_is_user_scoped() -> None:
    async def scenario() -> None:
        redis = _fake()
        fanout = WsFanout(redis, org="titan")
        payload = {
            "type": "notification.created",
            "user_id": _UID,
            "notification": {"id": "n1", "title": "hi"},
        }
        published = dict(await _collect(fanout, redis, payload))
        assert list(published) == [channels.user_channel(_UID)]  # ONLY that user's channel
        assert published[channels.user_channel(_UID)]["type"] == "notification.created"
        await redis.aclose()

    run_async(scenario)


def test_sys_notice_project_scoped_and_bad_payload_dropped() -> None:
    async def scenario() -> None:
        redis = _fake()
        fanout = WsFanout(redis, org="titan")
        published = dict(
            await _collect(
                fanout,
                redis,
                {"type": "sys.notice", "project_id": _PID, "level": "warn", "text": "!"},
            )
        )
        assert published[channels.project_channel(_PID)]["payload"] == {
            "level": "warn",
            "text": "!",
        }
        # Malformed (missing task) and unknown types are dropped, not raised.
        assert await _collect(fanout, redis, {"type": "task.updated"}) == []
        assert await _collect(fanout, redis, {"type": "who.knows"}) == []
        await redis.aclose()

    run_async(scenario)


def test_process_batch_consumes_domain_stream() -> None:
    async def scenario() -> None:
        redis = _fake()
        bus = RedisStreamBus(redis)
        fanout = WsFanout(redis, org="titan")
        await bus.ensure_group(STREAM_EVENTS_DOMAIN, WS_FANOUT_GROUP)
        task = {"id": "t", "project_id": _PID}
        await bus.publish(STREAM_EVENTS_DOMAIN, {"type": "task.created", "task": task}, actor="api")

        processed = await process_ws_fanout_batch(bus, fanout, "c1", block_ms=50)
        assert processed == 1
        # The event was republished to the project channel (buffered).
        assert await redis.llen(channels.buffer_key(_PID)) == 1
        # Acked → a re-consume yields nothing.
        assert await process_ws_fanout_batch(bus, fanout, "c1", block_ms=50) == 0
        await redis.aclose()

    run_async(scenario)
