"""The WS fan-out consumer: domain events → Redis pub/sub channels (TITAN-BCK-007).

This is the *single* path from committed domain state to the sockets: it consumes
``stream:events.domain`` — the Phase-10 outbox stream every domain module already
writes to — as its own consumer group and republishes each event onto the Vol 3 §5
/ Vol 4 §5.2 pub/sub channels. Domain code never talks to a socket; it only writes
its outbox row (TITAN-BCK-005), and this consumer does the rest. Running it as a
distinct consumer group means it fans out independently of the existing
``titan-domain`` inbox consumer, and running one shared group across replicas means
each event is republished exactly once.

Per event it assigns a channel-scoped ``seq``, builds the fixed §5 envelope, buffers
project frames for replay (:mod:`titan.realtime.buffer`), publishes to the project /
user / agents / org channel, and mirrors milestone-ish project events to the
organization activity feed.
"""

import asyncio
import contextlib
from typing import Any, Final

import structlog
from redis.asyncio import Redis

from titan.events.bus import Delivery, QueueBus
from titan.events.envelope import Envelope
from titan.events.service import STREAM_EVENTS_DOMAIN
from titan.realtime import channels, protocol
from titan.realtime.buffer import ProjectBuffer

logger = structlog.get_logger("titan.realtime")

WS_FANOUT_GROUP: Final = "titan-ws-fanout"

# Project events that are also surfaced on the org-wide activity feed (Vol 4
# §5.2 Organization Overview). Deltas and per-message chatter are excluded.
_ORG_MIRROR_TYPES: Final = frozenset(
    {
        protocol.TASK_CREATED,
        protocol.TASK_UPDATED,
        protocol.RUN_STARTED,
        protocol.RUN_FINISHED,
        protocol.DECISION_RECORDED,
        protocol.SPRINT_UPDATED,
    }
)


class WsFanout:
    """Maps one domain envelope onto the WS pub/sub channels."""

    def __init__(self, redis: Redis, *, org: str) -> None:
        self._redis = redis
        self._org = org
        self._buffer = ProjectBuffer(redis)

    async def handle_envelope(self, envelope: Envelope) -> None:
        """Republish one domain event. A malformed/unknown payload is logged and
        dropped — never raised — so one bad event can't wedge the consumer."""
        payload = envelope.payload
        event_type = payload.get("type")
        ts = _ts(envelope)
        handler = _HANDLERS.get(event_type or "")
        if handler is None:
            logger.debug("ws.fanout.skip", type=event_type, event_id=envelope.event_id)
            return
        try:
            await handler(self, payload, ts)
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "ws.fanout.bad_payload", type=event_type, event_id=envelope.event_id, exc_info=True
            )

    # -- channel publishers -------------------------------------------------

    async def _publish_project(
        self, project_id: str, ws_type: str, payload: dict[str, Any], ts: str, *, buffered: bool
    ) -> None:
        seq = await self._buffer.next_project_seq(project_id)
        wire = protocol.encode(
            protocol.build_frame(ws_type, seq=seq, payload=payload, project_id=project_id, ts=ts)
        )
        if buffered:
            await self._buffer.append(project_id, wire)
        await self._redis.publish(channels.project_channel(project_id), wire)
        if ws_type in _ORG_MIRROR_TYPES:
            # Coarse org-overview descriptor ONLY — never the full domain DTO. The
            # org feed is gated on org-wide read (endpoint._subscribe), and keeping
            # the payload to {event, project_id} is defense in depth: even a
            # legitimate org subscriber gets no per-project body it shouldn't cache.
            await self._publish_org({"event": ws_type, "project_id": project_id}, ts)

    async def _publish_agents(self, payload: dict[str, Any], ts: str) -> None:
        seq = int(await self._redis.incr(channels.agents_seq_key()))
        wire = protocol.encode(
            protocol.build_frame(protocol.AGENT_STATUS, seq=seq, payload=payload, ts=ts)
        )
        await self._redis.publish(channels.AGENTS_STATUS_CHANNEL, wire)

    async def _publish_user(self, user_id: str, payload: dict[str, Any], ts: str) -> None:
        seq = int(await self._redis.incr(channels.user_seq_key(user_id)))
        wire = protocol.encode(
            protocol.build_frame("notification.created", seq=seq, payload=payload, ts=ts)
        )
        await self._redis.publish(channels.user_channel(user_id), wire)

    async def _publish_org(self, payload: dict[str, Any], ts: str) -> None:
        seq = int(await self._redis.incr(channels.org_seq_key(self._org)))
        wire = protocol.encode(
            protocol.build_frame("activity.created", seq=seq, payload=payload, ts=ts)
        )
        await self._redis.publish(channels.org_channel(self._org), wire)


# -- per-type handlers ------------------------------------------------------


async def _task(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    task = payload["task"]
    await fanout._publish_project(str(task["project_id"]), payload["type"], task, ts, buffered=True)


async def _run_lifecycle(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    run = payload["run"]
    await fanout._publish_project(str(run["project_id"]), payload["type"], run, ts, buffered=True)


async def _run_delta(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    project_id = str(payload["project_id"])
    delta = {k: payload[k] for k in ("run_id", "delta") if k in payload}
    if "agent_id" in payload:
        delta["agent_id"] = payload["agent_id"]
    await fanout._publish_project(project_id, protocol.RUN_OUTPUT_DELTA, delta, ts, buffered=False)


async def _message(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    message = payload["message"]
    await fanout._publish_project(
        str(message["project_id"]), protocol.MESSAGE_CREATED, message, ts, buffered=True
    )


async def _decision(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    decision = payload["decision"]
    await fanout._publish_project(
        str(decision["project_id"]), protocol.DECISION_RECORDED, decision, ts, buffered=True
    )


async def _sprint(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    sprint = payload["sprint"]
    await fanout._publish_project(
        str(sprint["project_id"]), protocol.SPRINT_UPDATED, sprint, ts, buffered=True
    )


async def _kanban(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    # Vol 6 §2.5 (TITAN-WFL-009): a task state change surfaced as a Kanban card
    # move. Carries the §2.5 lane so the board never sees the fine-grained
    # engineering statuses.
    card = payload["kanban"]
    await fanout._publish_project(
        str(card["project_id"]), protocol.TASK_KANBAN, card, ts, buffered=True
    )


async def _pipeline(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    # Vol 6 §2.1 (TITAN-WFL-001): a pipeline stage transition, mirrored to the
    # dashboard per project. The frame carries the current pipeline cursor + the
    # specific change (stage_complete / rejected / not_applicable / released / …).
    pipeline_state = payload["pipeline"]
    frame = {"pipeline": pipeline_state, "change": payload.get("change", {})}
    await fanout._publish_project(
        str(pipeline_state["project_id"]), protocol.PIPELINE_UPDATED, frame, ts, buffered=True
    )


async def _agent_status(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    await fanout._publish_agents(
        {k: payload.get(k) for k in ("agent_id", "state", "current_task_id")}, ts
    )


async def _notification(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    await fanout._publish_user(str(payload["user_id"]), payload["notification"], ts)


async def _sys_notice(fanout: WsFanout, payload: dict[str, Any], ts: str) -> None:
    project_id = payload.get("project_id")
    notice = {"level": payload.get("level", "info"), "text": payload.get("text", "")}
    if project_id is not None:
        await fanout._publish_project(
            str(project_id), protocol.SYS_NOTICE, notice, ts, buffered=True
        )


_HANDLERS: dict[str, Any] = {
    protocol.TASK_CREATED: _task,
    protocol.TASK_UPDATED: _task,
    protocol.RUN_STARTED: _run_lifecycle,
    protocol.RUN_FINISHED: _run_lifecycle,
    protocol.RUN_OUTPUT_DELTA: _run_delta,
    protocol.MESSAGE_CREATED: _message,
    protocol.DECISION_RECORDED: _decision,
    protocol.SPRINT_UPDATED: _sprint,
    protocol.AGENT_STATUS: _agent_status,
    protocol.TASK_KANBAN: _kanban,
    protocol.PIPELINE_UPDATED: _pipeline,
    "notification.created": _notification,
    protocol.SYS_NOTICE: _sys_notice,
}


def _ts(envelope: Envelope) -> str:
    occurred = envelope.occurred_at
    return occurred.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# -- consumer loop ----------------------------------------------------------


async def process_ws_fanout_batch(
    bus: QueueBus, fanout: WsFanout, consumer: str, *, block_ms: int = 1000
) -> int:
    """Consume one batch of domain events and republish each; ack on success,
    nack on failure so the reaper retries (and eventually dead-letters poison)."""
    deliveries = await bus.consume(
        STREAM_EVENTS_DOMAIN, WS_FANOUT_GROUP, consumer, count=100, block_ms=block_ms
    )
    for delivery in deliveries:
        await _handle_delivery(bus, fanout, delivery)
    return len(deliveries)


async def _handle_delivery(bus: QueueBus, fanout: WsFanout, delivery: Delivery) -> None:
    try:
        await fanout.handle_envelope(delivery.envelope)
        await bus.ack(STREAM_EVENTS_DOMAIN, WS_FANOUT_GROUP, delivery)
    except Exception:
        logger.exception("ws.fanout.delivery_failed", event_id=delivery.envelope.event_id)
        await bus.nack(STREAM_EVENTS_DOMAIN, WS_FANOUT_GROUP, delivery)


async def run_ws_fanout(  # pragma: no cover — long-running loop, live-verified
    bus: QueueBus, fanout: WsFanout, consumer: str, *, stop: asyncio.Event
) -> None:
    """Republish domain events to the WS channels until stopped. Self-healing:
    a transient Redis/DB error is logged and retried, never propagated — so a
    Redis that is briefly unavailable at boot never fails app startup. The group
    is (re)ensured inside the loop (idempotent) rather than once up front."""
    while not stop.is_set():
        try:
            await bus.ensure_group(STREAM_EVENTS_DOMAIN, WS_FANOUT_GROUP)
            processed = await process_ws_fanout_batch(bus, fanout, consumer)
        except Exception:
            logger.exception("ws.fanout.loop_error")
            await _sleep_or_stop(stop, 2.0)
            continue
        if not processed:
            await _sleep_or_stop(stop, 0.05)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:  # pragma: no cover
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


__all__ = [
    "WS_FANOUT_GROUP",
    "WsFanout",
    "process_ws_fanout_batch",
    "run_ws_fanout",
]
