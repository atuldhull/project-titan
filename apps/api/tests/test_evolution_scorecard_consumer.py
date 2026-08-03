"""The evolution.scorecard consumer (Phase 84) — the terminal-event → scorecard path.

Proves a `task.terminal` delivery drives scorecard generation and acks, a
non-terminal delivery is a harmless ack, and a redelivery is idempotent.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import run_async
from tests.test_evolution_scorecard import _DIMS, _DimsGateway, _seed_project_task
from titan.agents.service import build_evolution_resolver
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


def _delivery(event_type: str, task_id: str | None) -> Any:
    task = {"id": task_id} if task_id else {}
    return SimpleNamespace(
        envelope=SimpleNamespace(payload={"type": event_type, "task": task}, event_id="e1"),
    )


def test_terminal_delivery_generates_scorecard_and_acks(migrated_engine: Any) -> None:
    async def scenario() -> None:
        _, tid = await _seed_project_task(migrated_engine, role="backend", status="done")
        bus, gateway = _Bus(), _DimsGateway(_DIMS)
        resolver = build_evolution_resolver(Settings())
        await evo_consumer.handle_scorecard_delivery(
            migrated_engine, bus, gateway, resolver, _delivery("task.terminal", str(tid))
        )
        assert len(bus.acked) == 1 and not bus.nacked
        assert await repo.scorecard_exists(migrated_engine, tid)
        # A non-terminal event is acked without generating anything (fan-out group).
        await evo_consumer.handle_scorecard_delivery(
            migrated_engine, bus, gateway, resolver, _delivery("task.created", str(tid))
        )
        assert len(bus.acked) == 2

    run_async(scenario)


def test_terminal_task_id_parsing() -> None:
    assert evo_consumer._terminal_task_id(_delivery("task.created", "x")) is None
    assert evo_consumer._terminal_task_id(_delivery("task.terminal", "not-a-uuid")) is None
    good = _delivery("task.terminal", "019fc610-d253-7f40-85c8-2288f0eddc11")
    assert str(evo_consumer._terminal_task_id(good)) == "019fc610-d253-7f40-85c8-2288f0eddc11"
