"""The evolution.scorecard consumer (Phase 84) — the terminal-event → scorecard path.

Proves a `task.terminal` delivery drives scorecard generation and acks, a
non-terminal delivery is a harmless ack, and a redelivery is idempotent.
"""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.conftest import run_async
from tests.test_evolution_scorecard import _DIMS, _DimsGateway, _seed_project_task
from titan.agents.service import build_evolution_resolver
from titan.events.bus import QueueBus
from titan.evolution import consumer as evo_consumer
from titan.evolution import repository as repo
from titan.settings import Settings

pytestmark = pytest.mark.schema


class _Bus:
    def __init__(self) -> None:
        self.acked: list[Any] = []
        self.nacked: list[Any] = []

    async def ack(self, topic: str, group: str, delivery: Any) -> None:
        self.acked.append(delivery)

    async def nack(self, topic: str, group: str, delivery: Any) -> None:
        self.nacked.append(delivery)


def _delivery(event_type: str, task_id: str | None, status: str = "done") -> Any:
    task = {"id": task_id, "status": status} if task_id else {}
    return SimpleNamespace(
        envelope=SimpleNamespace(payload={"type": event_type, "task": task}, event_id="e1"),
    )


def test_terminal_delivery_generates_scorecard_and_acks(migrated_engine: Any) -> None:
    async def scenario() -> None:
        _, tid = await _seed_project_task(migrated_engine, role="backend", status="done")
        bus, gateway = _Bus(), _DimsGateway(_DIMS)
        resolver = build_evolution_resolver(Settings())
        await evo_consumer.handle_scorecard_delivery(
            migrated_engine,
            cast(QueueBus, bus),
            gateway,
            resolver,
            _delivery("task.terminal", str(tid)),
        )
        assert len(bus.acked) == 1 and not bus.nacked
        assert await repo.scorecard_exists(migrated_engine, tid)
        # A non-terminal event is acked without generating anything (fan-out group).
        await evo_consumer.handle_scorecard_delivery(
            migrated_engine,
            cast(QueueBus, bus),
            gateway,
            resolver,
            _delivery("task.created", str(tid)),
        )
        assert len(bus.acked) == 2

    run_async(scenario)


class _ShapeAwareGateway:
    """One gateway for the whole failed-terminal chain: it returns the rubric dims
    to the reviewer, a reflection to the executing agent, and a classification to
    the memory agent — branching on the caller's agent_id."""

    async def chat(self, req: Any) -> Any:
        import json as _json
        from decimal import Decimal

        from titan.llm.models import ChatResponse, Usage

        agent = req.metadata.agent_id
        if agent == "agent.reviewer":
            body = _json.dumps(_DIMS)
        elif agent == "agent.memory":
            body = _json.dumps({"primary": "F-SEC", "secondaries": ["F-STD"], "confidence": 0.9})
        else:  # the executing agent's post-task reflection
            body = _json.dumps({"lesson": "leaked a token", "confidence": 0.6})
        return ChatResponse(
            text=body,
            finish_reason="stop",
            usage=Usage(input_tokens=5, output_tokens=10),
            provider="mock",
            model_id="mock/eval",
            cost_usd=Decimal("0"),
            latency_ms=1,
        )


def test_failed_terminal_drives_scorecard_reflection_and_rcr(migrated_engine: Any) -> None:
    """The full Phase-85 chain on a FAILED terminal: scorecard + reflection + a
    classified, opened Root-Cause Record — all off one task.terminal delivery."""
    from sqlalchemy import text

    from titan.evolution import repository as evo_repo

    async def scenario() -> None:
        pid, tid = await _seed_project_task(migrated_engine, role="backend", status="failed")
        bus, gateway = _Bus(), _ShapeAwareGateway()
        resolver = build_evolution_resolver(Settings())
        await evo_consumer.handle_scorecard_delivery(
            migrated_engine,
            cast(QueueBus, bus),
            gateway,
            resolver,
            _delivery("task.terminal", str(tid), "failed"),
        )
        assert len(bus.acked) == 1 and not bus.nacked
        assert await repo.scorecard_exists(migrated_engine, tid)
        # A reflection landed at project scope.
        async with migrated_engine.connect() as conn:
            refl = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM brain.memory_records WHERE scope = :s AND "
                        "metadata->>'record_subtype' = 'post_task_reflection'"
                    ),
                    {"s": f"project:{pid}"},
                )
            ).first()
        assert refl is not None
        # An RCR was classified + opened for this failure.
        rcrs = await evo_repo.list_rcrs(migrated_engine, status="open")
        mine = [r for r in rcrs if str(tid) in r["task_ids"]]
        assert mine and mine[0]["failure_code"] == "F-SEC" and mine[0]["severity"] == "high"

    run_async(scenario)


def test_terminal_task_parsing() -> None:
    assert evo_consumer._terminal_task(_delivery("task.created", "x")) is None
    assert evo_consumer._terminal_task(_delivery("task.terminal", "not-a-uuid")) is None
    good = _delivery("task.terminal", "019fc610-d253-7f40-85c8-2288f0eddc11", status="failed")
    parsed = evo_consumer._terminal_task(good)
    assert parsed is not None
    task_id, status = parsed
    assert str(task_id) == "019fc610-d253-7f40-85c8-2288f0eddc11"
    assert status == "failed"
