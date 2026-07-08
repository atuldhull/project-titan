"""The agent message bus (Vol 2 §6.3, TITAN-AGT-003/016).

Delivery reuses the Phase-10 transactional outbox + QueueBus — there is exactly
one delivery mechanism. :func:`send` writes the :class:`AgentMessage` to
``messages`` (the durable record) and appends an outbox row in the SAME
transaction, keyed on ``message_id``; the outbox drainer then publishes it to the
recipient's inbox stream ``titan:{project_id}:inbox:{to_agent}``. So nothing an
agent "sent" lives only in Redis: if Redis dies mid-conversation, replay from
PostgreSQL is complete (TITAN-AGT-016). Consumers are idempotent on ``message_id``
(the published event_id), and ``requires_ack`` messages left unacked past their
type-specific timeout re-enter the pending set (the reaper reclaims them) and
fire the watchdog hook — the full watchdog lands in Phase 32.
"""

import json
import uuid
from collections.abc import Awaitable, Callable

import structlog
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.events.bus import Delivery, QueueBus
from titan.events.service import write_event
from titan.messaging import repository as repo
from titan.messaging.protocol import AgentMessage

logger = structlog.get_logger("titan.messaging.bus")
_TRACER = trace.get_tracer("titan.messaging")

# One consumer group per agent *runtime* (§6.3): all workers running a given
# agent share this group and compete for its inbox, at-least-once.
RUNTIME_GROUP = "runtime"

# Per-type ack timeouts (ms). A requires_ack message unacked past its timeout
# re-enters the pending set; the Phase-32 watchdog reads these to decide when to
# reclaim + escalate. Absent an entry, DEFAULT_ACK_TIMEOUT_MS applies.
DEFAULT_ACK_TIMEOUT_MS = 300_000
ACK_TIMEOUTS_MS: dict[str, int] = {
    "TaskAssignment": 600_000,
    "ReviewRequest": 300_000,
    "ArchitectureDiscussion": 900_000,
    "ConflictResolution": 600_000,
}


def ack_timeout_ms(message_type: str) -> int:
    return ACK_TIMEOUTS_MS.get(message_type, DEFAULT_ACK_TIMEOUT_MS)


def inbox_stream(project_id: uuid.UUID | str, agent_id: str) -> str:
    """The recipient's inbox stream/topic (§6.3)."""
    return f"titan:{project_id}:inbox:{agent_id}"


# --- watchdog hook (seam; full watchdog is Phase 32) -----------------------

WatchdogHook = Callable[[AgentMessage], Awaitable[None]]
_watchdog_hooks: list[WatchdogHook] = []


def register_watchdog(hook: WatchdogHook) -> None:
    """Register a callback fired for each requires_ack message reclaimed unacked
    past its timeout. Phase 32's Orchestrator watchdog registers here."""
    _watchdog_hooks.append(hook)


def clear_watchdogs() -> None:
    """Drop all hooks (test isolation)."""
    _watchdog_hooks.clear()


# --- send ------------------------------------------------------------------


async def send(conn: AsyncConnection, message: AgentMessage, *, actor: str) -> uuid.UUID:
    """Persist ``message`` and enqueue it to the recipient's inbox, atomically.

    Runs inside the caller's transaction: the ``messages`` row and the outbox row
    commit together, so a persisted message is always eventually delivered and a
    delivered message is always persisted. Returns the message_id.
    """
    thread_id = message.thread_id or message.correlation_id
    await repo.insert_message(
        conn,
        message_id=message.message_id,
        project_id=message.project_id,
        thread_id=thread_id,
        kind=message.kind,
        sender_agent_id=None,  # bus messages identify their sender by role
        sender_user_id=None,
        body=json.dumps(message.model_dump(mode="json")["payload"]),
        task_id=message.task_id,
        schema_version=message.schema_version,
        correlation_id=message.correlation_id,
        from_agent=message.from_agent,
        to_agent=message.to_agent,
        priority=message.priority,
        requires_ack=message.requires_ack,
    )
    stream = inbox_stream(message.project_id, message.to_agent)
    with _TRACER.start_as_current_span("agent.message") as span:
        # Envelope metadata ONLY — never payload content in traces (TITAN-OPS-016).
        span.set_attribute("messaging.system", "titan")
        span.set_attribute("messaging.type", message.type)
        span.set_attribute("messaging.from_agent", message.from_agent)
        span.set_attribute("messaging.to_agent", message.to_agent)
        span.set_attribute("messaging.correlation_id", str(message.correlation_id))
        span.set_attribute("messaging.priority", message.priority)
        span.set_attribute("messaging.requires_ack", message.requires_ack)
        await write_event(
            conn,
            stream,
            message.model_dump(mode="json"),
            actor=actor,
            event_id=message.message_id,
        )
    logger.info(
        "agent_message.sent",
        message_id=str(message.message_id),
        type=message.type,
        from_agent=message.from_agent,
        to_agent=message.to_agent,
    )
    return message.message_id


# --- consume / ack ---------------------------------------------------------


async def ensure_inbox(bus: QueueBus, project_id: uuid.UUID | str, agent_id: str) -> None:
    await bus.ensure_group(inbox_stream(project_id, agent_id), RUNTIME_GROUP)


async def consume_inbox(
    bus: QueueBus,
    project_id: uuid.UUID | str,
    agent_id: str,
    consumer: str,
    *,
    count: int = 10,
    block_ms: int = 1000,
) -> list[Delivery]:
    """Read new inbox messages for one agent runtime (dedup'd on message_id)."""
    stream = inbox_stream(project_id, agent_id)
    await bus.ensure_group(stream, RUNTIME_GROUP)
    return await bus.consume(stream, RUNTIME_GROUP, consumer, count=count, block_ms=block_ms)


async def ack_inbox(
    bus: QueueBus, project_id: uuid.UUID | str, agent_id: str, delivery: Delivery
) -> None:
    await bus.ack(inbox_stream(project_id, agent_id), RUNTIME_GROUP, delivery)


async def reclaim_unacked(
    bus: QueueBus,
    project_id: uuid.UUID | str,
    agent_id: str,
    consumer: str,
    *,
    min_idle_ms: int = DEFAULT_ACK_TIMEOUT_MS,
) -> list[Delivery]:
    """Reclaim idle-pending (unacked) inbox messages. For each reclaimed
    ``requires_ack`` message, fire the watchdog hooks — the seam the Phase-32
    watchdog uses to escalate a stuck conversation. Returns the reclaimed
    deliveries so the caller reprocesses them (at-least-once)."""
    stream = inbox_stream(project_id, agent_id)
    # Self-sufficient like consume_inbox: a reclaim that runs before the first
    # consume (e.g. a worker loop that reclaims first) must not NOGROUP-crash.
    await bus.ensure_group(stream, RUNTIME_GROUP)
    reclaimed = await bus.reap(stream, RUNTIME_GROUP, consumer, min_idle_ms=min_idle_ms)
    for delivery in reclaimed:
        try:
            message = AgentMessage.model_validate(delivery.envelope.payload)
        except ValueError:  # a malformed reclaimed payload — skip the hook, keep the delivery
            logger.warning("agent_message.reclaim_unparseable", event_id=delivery.envelope.event_id)
            continue
        if message.requires_ack:
            for hook in _watchdog_hooks:
                await hook(message)
    return reclaimed


__all__ = [
    "ACK_TIMEOUTS_MS",
    "DEFAULT_ACK_TIMEOUT_MS",
    "RUNTIME_GROUP",
    "WatchdogHook",
    "ack_inbox",
    "ack_timeout_ms",
    "clear_watchdogs",
    "consume_inbox",
    "ensure_inbox",
    "inbox_stream",
    "reclaim_unacked",
    "register_watchdog",
    "send",
]
