"""The Agent Runtime worker (Vol 2 §8, TITAN-AGT-002).

A pool of these drives the stateless turn loop: each worker owns one agent's
inbox on one runtime, does XREADGROUP → :func:`run_turn` → XACK, and reclaims
messages abandoned by a dead worker. Because turns are stateless and emissions
are idempotent, killing and restarting a worker mid-exchange loses no accepted
work and duplicates no side effect — a crash before XACK simply re-enters the
message into the pending set, where this or another worker reclaims and
reprocesses it (the ``message_exists`` guard makes the re-emission a no-op).

The bounded :meth:`process_available` / :meth:`process_reclaimed` methods are the
unit the app lifespan schedules and tests drive directly; :meth:`run_forever`
loops them for a live worker until asked to stop.
"""

import asyncio
import uuid
from dataclasses import dataclass

import structlog
from pydantic import ValidationError

from titan.events.service import Delivery, QueueBus
from titan.messaging.service import (
    ack_inbox,
    consume_inbox,
    parse_agent_message,
    reclaim_unacked,
)
from titan.runtime.turn import AgentRuntime, TurnResult, run_turn

logger = structlog.get_logger("titan.runtime.worker")


@dataclass
class RuntimeWorker:
    """One worker serving ``agent_id``'s inbox for ``project_id``."""

    runtime: AgentRuntime
    bus: QueueBus
    project_id: uuid.UUID
    agent_id: str
    consumer: str = "runtime-1"

    async def process_available(
        self, *, max_turns: int = 100, block_ms: int = 50
    ) -> list[TurnResult]:
        """Process the messages currently waiting in this agent's inbox (bounded).
        Returns one :class:`TurnResult` per processed message."""
        results: list[TurnResult] = []
        while len(results) < max_turns:
            deliveries = await consume_inbox(
                self.bus, self.project_id, self.agent_id, self.consumer, count=10, block_ms=block_ms
            )
            if not deliveries:
                break
            for delivery in deliveries:
                outcome = await self._process(delivery)
                if outcome is not None:
                    results.append(outcome)
        return results

    async def process_reclaimed(
        self, *, min_idle_ms: int = 300_000, consumer: str | None = None
    ) -> list[TurnResult]:
        """Reprocess messages abandoned unacked by a dead worker (the crash-
        recovery path). Idempotent by construction."""
        reclaimed = await reclaim_unacked(
            self.bus,
            self.project_id,
            self.agent_id,
            consumer or self.consumer,
            min_idle_ms=min_idle_ms,
        )
        results: list[TurnResult] = []
        for delivery in reclaimed:
            outcome = await self._process(delivery)
            if outcome is not None:
                results.append(outcome)
        return results

    async def _process(self, delivery: Delivery) -> TurnResult | None:
        try:
            trigger = parse_agent_message(delivery.envelope.payload)
        except ValidationError:
            # A malformed inbox payload is undeliverable — ack it so it does not
            # poison-loop (parse_agent_message already logged the safe metadata).
            await ack_inbox(self.bus, self.project_id, self.agent_id, delivery)
            return None
        result = await run_turn(self.runtime, self.project_id, self.agent_id, trigger)
        if result.should_ack:
            await ack_inbox(self.bus, self.project_id, self.agent_id, delivery)
        else:
            logger.warning(
                "runtime.turn_left_pending",
                agent_id=self.agent_id,
                message_id=str(trigger.message_id),
                outcome=result.outcome,
            )
        return result

    async def run_forever(
        self, *, stop: asyncio.Event, poll_block_ms: int = 1000, reclaim_min_idle_ms: int = 300_000
    ) -> None:
        """Live loop: process available messages, periodically reclaim abandoned
        ones, until ``stop`` is set. Each iteration is a bounded, restart-safe
        unit — a kill between iterations loses nothing."""
        while not stop.is_set():
            await self.process_reclaimed(min_idle_ms=reclaim_min_idle_ms)
            processed = await self.process_available(block_ms=poll_block_ms)
            if not processed:
                # Nothing to do — yield so a cancel/stop is observed promptly.
                await asyncio.sleep(0)


__all__ = ["RuntimeWorker"]
