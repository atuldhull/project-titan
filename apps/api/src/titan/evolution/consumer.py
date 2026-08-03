"""Evolution post-terminal consumers (Phase 84/85).

`evolution-scorecard` (fan-out on stream:events.domain) reacts to the ONE
`task.terminal` event: it generates the scorecard (Phase 84), fires the executing
agent's post-task reflection (EVO-008), and — for a FAILED terminal — classifies
the failure and opens/reopens a Root-Cause Record (EVO-011/012). `evolution-retro`
reacts to `pipeline.updated` with `change.released` to generate the post-project
retrospective (EVO-009). All steps are idempotent + fail-safe: none may block the
transition (which already committed off-transaction).
"""

import asyncio
import contextlib
import uuid
from typing import Any

import structlog

from titan.events.bus import Delivery, QueueBus
from titan.events.streams import STREAM_EVENTS_DOMAIN
from titan.evolution import failures, reflections, repository, scoring
from titan.evolution.resolver import EvolutionResolver
from titan.observability.metrics import EVOLUTION_OPS, STREAM_DEPTH

logger = structlog.get_logger("titan.evolution.consumer")

SCORECARD_GROUP = "evolution-scorecard"
RETRO_GROUP = "evolution-retro"
_FAILURE_STATES = ("failed",)  # high-severity terminal that warrants an RCR


def _terminal_task(delivery: Delivery) -> tuple[uuid.UUID, str] | None:
    payload = delivery.envelope.payload
    if payload.get("type") != "task.terminal":
        return None
    task = payload.get("task")
    if not isinstance(task, dict):
        return None
    try:
        return uuid.UUID(str(task.get("id"))), str(task.get("status", ""))
    except (ValueError, TypeError):
        return None


def _released_project(delivery: Delivery) -> uuid.UUID | None:
    payload = delivery.envelope.payload
    if payload.get("type") != "pipeline.updated":
        return None
    change = payload.get("change")
    if not isinstance(change, dict) or not change.get("released"):
        return None
    pid = payload.get("project_id") or change.get("project_id")
    try:
        return uuid.UUID(str(pid))
    except (ValueError, TypeError):
        return None


async def handle_scorecard_delivery(
    engine: Any, bus: QueueBus, gateway: Any, resolver: EvolutionResolver, delivery: Delivery
) -> None:
    """On task.terminal: scorecard → reflection → (on failure) classify + RCR. Acks."""
    try:
        parsed = _terminal_task(delivery)
        if parsed is not None:
            task_id, status = parsed
            # On a redelivery the scorecard already exists (insert returns None); fetch
            # it so the idempotent post-scorecard steps still run instead of being
            # silently skipped — the nack→reap retry path cannot otherwise re-run them.
            scorecard = await scoring.generate_scorecard(
                engine, gateway, resolver, task_id=task_id
            ) or await repository.get_scorecard(engine, task_id)
            EVOLUTION_OPS.labels("scorecard", "ok" if scorecard is not None else "failed").inc()
            if scorecard is not None:
                await reflections.reflect_on_scorecard(engine, gateway, resolver, scorecard)
                if status in _FAILURE_STATES:
                    await _rcr_on_failure(engine, gateway, resolver, scorecard, task_id)
        await bus.ack(STREAM_EVENTS_DOMAIN, SCORECARD_GROUP, delivery)
    except Exception:
        logger.exception("scorecard.consume_failed", event_id=delivery.envelope.event_id)
        await bus.nack(STREAM_EVENTS_DOMAIN, SCORECARD_GROUP, delivery)


async def _rcr_on_failure(
    engine: Any,
    gateway: Any,
    resolver: EvolutionResolver,
    scorecard: dict[str, Any],
    task_id: uuid.UUID,
) -> None:
    """Classify a failed task + open/reopen a high-severity RCR (EVO-011/012)."""
    agent = scorecard["agent_id"]
    classification = await failures.classify_failure(
        gateway, resolver, f"Task failed. Executor={agent}, class={scorecard['task_class']}."
    )
    code = classification["primary"]
    await failures.open_or_reopen_rcr(
        engine,
        failure_code=code,
        severity="high",
        signature=f"{code}:{agent}",
        summary=f"{code} failure by {agent} ({scorecard['task_class']})",
        task_ids=[task_id],
        contributing={
            "prompt_sha": scorecard["prompt_sha"],
            "model_id": scorecard["model_id"],
            "secondaries": classification["secondaries"],
        },
    )


async def handle_retrospective_delivery(
    engine: Any, bus: QueueBus, gateway: Any, resolver: EvolutionResolver, delivery: Delivery
) -> None:
    """On pipeline.updated(released): generate the post-project retrospective. Acks."""
    try:
        pid = _released_project(delivery)
        if pid is not None:
            await reflections.generate_retrospective(engine, gateway, resolver, project_id=pid)
        await bus.ack(STREAM_EVENTS_DOMAIN, RETRO_GROUP, delivery)
    except Exception:
        logger.exception("retrospective.consume_failed", event_id=delivery.envelope.event_id)
        await bus.nack(STREAM_EVENTS_DOMAIN, RETRO_GROUP, delivery)


async def process_scorecard_batch(
    engine: Any,
    bus: QueueBus,
    gateway: Any,
    resolver: EvolutionResolver,
    consumer: str,
    *,
    block_ms: int = 1000,
) -> int:
    deliveries = await bus.consume(
        STREAM_EVENTS_DOMAIN, SCORECARD_GROUP, consumer, count=50, block_ms=block_ms
    )
    for delivery in deliveries:
        await handle_scorecard_delivery(engine, bus, gateway, resolver, delivery)
    return len(deliveries)


async def process_retro_batch(
    engine: Any,
    bus: QueueBus,
    gateway: Any,
    resolver: EvolutionResolver,
    consumer: str,
    *,
    block_ms: int = 1000,
) -> int:
    deliveries = await bus.consume(
        STREAM_EVENTS_DOMAIN, RETRO_GROUP, consumer, count=50, block_ms=block_ms
    )
    for delivery in deliveries:
        await handle_retrospective_delivery(engine, bus, gateway, resolver, delivery)
    return len(deliveries)


async def _reap(
    engine: Any,
    bus: QueueBus,
    gateway: Any,
    resolver: EvolutionResolver,
    consumer: str,
    group: str,
    handler: Any,
) -> int:
    reclaimed = await bus.reap(STREAM_EVENTS_DOMAIN, group, consumer, min_idle_ms=60_000)
    for delivery in reclaimed:
        await handler(engine, bus, gateway, resolver, delivery)
    STREAM_DEPTH.labels(stream=STREAM_EVENTS_DOMAIN, group=group).set(
        await bus.pending_depth(STREAM_EVENTS_DOMAIN, group)
    )
    return len(reclaimed)


async def run_scorecard_consumer(  # pragma: no cover - loop verified via its units
    engine: Any,
    bus: QueueBus,
    gateway: Any,
    resolver: EvolutionResolver,
    consumer: str,
    *,
    stop: asyncio.Event,
) -> None:
    await bus.ensure_group(STREAM_EVENTS_DOMAIN, SCORECARD_GROUP)
    while not stop.is_set():
        try:
            processed = await process_scorecard_batch(engine, bus, gateway, resolver, consumer)
        except Exception:
            logger.exception("scorecard.consumer_loop_error")
            await _sleep_or_stop(stop, 2.0)
            continue
        if not processed:
            await _sleep_or_stop(stop, 0.5)


async def run_scorecard_reaper(  # pragma: no cover - loop verified via its units
    engine: Any,
    bus: QueueBus,
    gateway: Any,
    resolver: EvolutionResolver,
    consumer: str,
    *,
    stop: asyncio.Event,
) -> None:
    await bus.ensure_group(STREAM_EVENTS_DOMAIN, SCORECARD_GROUP)
    while not stop.is_set():
        try:
            await _reap(
                engine, bus, gateway, resolver, consumer, SCORECARD_GROUP, handle_scorecard_delivery
            )
        except Exception:
            logger.exception("scorecard.reaper_loop_error")
        await _sleep_or_stop(stop, 30.0)


async def run_retro_consumer(  # pragma: no cover - loop verified via its units
    engine: Any,
    bus: QueueBus,
    gateway: Any,
    resolver: EvolutionResolver,
    consumer: str,
    *,
    stop: asyncio.Event,
) -> None:
    await bus.ensure_group(STREAM_EVENTS_DOMAIN, RETRO_GROUP)
    while not stop.is_set():
        try:
            processed = await process_retro_batch(engine, bus, gateway, resolver, consumer)
        except Exception:
            logger.exception("retro.consumer_loop_error")
            await _sleep_or_stop(stop, 2.0)
            continue
        if not processed:
            await _sleep_or_stop(stop, 0.5)


async def run_retro_reaper(  # pragma: no cover - loop verified via its units
    engine: Any,
    bus: QueueBus,
    gateway: Any,
    resolver: EvolutionResolver,
    consumer: str,
    *,
    stop: asyncio.Event,
) -> None:
    await bus.ensure_group(STREAM_EVENTS_DOMAIN, RETRO_GROUP)
    while not stop.is_set():
        try:
            await _reap(
                engine, bus, gateway, resolver, consumer, RETRO_GROUP, handle_retrospective_delivery
            )
        except Exception:
            logger.exception("retro.reaper_loop_error")
        await _sleep_or_stop(stop, 30.0)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:  # pragma: no cover
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


__all__ = [
    "RETRO_GROUP",
    "SCORECARD_GROUP",
    "handle_retrospective_delivery",
    "handle_scorecard_delivery",
    "process_retro_batch",
    "process_scorecard_batch",
    "run_retro_consumer",
    "run_retro_reaper",
    "run_scorecard_consumer",
    "run_scorecard_reaper",
]
