"""Phase 14 fault-injection tests: the worker run lifecycle (§8.2).

DoD: a successful run yields persisted run + artifact rows and transitions its
task to in_review; a timing-out run retries twice then dead-letters (run rows
show timed_out, DLQ depth rises); a run whose worker dies mid-flight is reclaimed
by a peer via XAUTOCLAIM and completed with no duplicate side effects.

Drives the coordinator units directly against the real DB + fakeredis (which
implements Redis Streams: xreadgroup/xautoclaim/xpending)."""

import asyncio
import uuid
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.events.outbox import OutboxDrainer
from titan.events.redis_bus import RedisStreamBus
from titan.events.service import STREAM_RUNS_EXECUTE, STREAM_RUNS_RESULTS, dlq_stream
from titan.runs import coordinator
from titan.runs import service as runs
from titan.runs.executor import RunContext, RunOutcome, StubExecutor
from titan.runs.storage import FilesystemStorage

pytestmark = pytest.mark.schema

_MB = {"provider": "ollama", "model": "q"}


@pytest.fixture(autouse=True)
def _clean_outbox(api: dict[str, Any]) -> None:
    """Each test drains its own outbox into a fresh fakeredis, so start from an
    empty outbox — other tests share this session DB and leave undrained events
    (task.* and runs.results) that would otherwise pollute this test's streams."""

    async def clean() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(text("DELETE FROM outbox"))

    run_async(clean)


def _agent(api: dict[str, Any], role: str = "backend") -> str:
    r = api["client"].post(
        "/api/v1/agents",
        json={"role": role, "name": f"{role}-bot", "model_binding": _MB},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _transition(api: dict[str, Any], task_id: str, action: str, actor: str | None = None) -> Any:
    body: dict[str, Any] = {"action": action}
    if actor is not None:
        body["actor_agent_id"] = actor
    return api["client"].post(
        f"/api/v1/tasks/{task_id}:transition", json=body, headers=api["token_for"]("owner")
    )


def _setup_assigned_run(api: dict[str, Any]) -> tuple[str, str, str, str]:
    """A project + agent + task driven to `assigned`, with a queued run."""
    name = f"proj-{uuid.uuid4().hex[:12]}"
    pid = (
        api["client"]
        .post(
            "/api/v1/projects",
            json={"name": name, "display_name": name, "brief": "b"},
            headers=api["token_for"]("owner"),
        )
        .json()["id"]
    )
    agent = _agent(api)
    tid = (
        api["client"]
        .post(
            f"/api/v1/projects/{pid}/tasks",
            json={"title": "impl", "spec": "build the thing"},
            headers=api["token_for"]("owner"),
        )
        .json()["id"]
    )
    assert _transition(api, tid, "ready").status_code == 200
    assert _transition(api, tid, "assign", agent).status_code == 200
    run_id = (
        api["client"]
        .post(
            f"/api/v1/tasks/{tid}/runs",
            json={"agent_id": agent, "input": {"prompt_ref": "ctx-1"}},
            headers=api["token_for"]("owner"),
        )
        .json()["id"]
    )
    return str(pid), str(tid), str(agent), str(run_id)


def _run_row(api: dict[str, Any], run_id: str) -> dict[str, Any]:
    body: dict[str, Any] = (
        api["client"].get(f"/api/v1/runs/{run_id}", headers=api["token_for"]("owner")).json()
    )
    return body


def _artifacts(api: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    body = (
        api["client"]
        .get(f"/api/v1/runs/{run_id}/artifacts", headers=api["token_for"]("owner"))
        .json()
    )
    items: list[dict[str, Any]] = body["items"]
    return items


def _task_status(api: dict[str, Any], task_id: str) -> str:
    return str(
        api["client"]
        .get(f"/api/v1/tasks/{task_id}", headers=api["token_for"]("owner"))
        .json()["status"]
    )


class _SlowExecutor:
    """Never completes within the soft timeout — always trips a timeout."""

    async def execute(self, ctx: RunContext) -> RunOutcome:
        await asyncio.sleep(30)
        raise AssertionError("should have timed out")  # pragma: no cover


def test_successful_run_persists_artifacts_and_submits_for_review(
    api: dict[str, Any], tmp_path: Any
) -> None:
    pid, tid, agent, run_id = _setup_assigned_run(api)

    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamBus(redis)
        storage = FilesystemStorage(tmp_path)
        await bus.ensure_group(STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP)
        await bus.ensure_group(STREAM_RUNS_RESULTS, coordinator.RUNS_RESULTS_GROUP)
        await bus.publish(
            STREAM_RUNS_EXECUTE,
            {"run_id": run_id, "task_id": tid, "agent_id": agent},
            actor="test",
        )
        processed = await coordinator.process_execute_batch(
            api["engine"], bus, storage, StubExecutor(), "w1", concurrency=4, soft_timeout=5.0
        )
        assert processed == 1
        # The result was written to the outbox; drain it to stream:runs.results
        # and let the results consumer drive the state machine.
        await OutboxDrainer(api["engine"], bus).drain_once()
        await coordinator.process_results_batch(api["engine"], bus, "w1")
        await redis.aclose()

    run_async(scenario)

    run = _run_row(api, run_id)
    assert run["status"] == "succeeded"
    assert run["tokens_in"] == 128 and run["tokens_out"] == 256
    assert run["provider"] == "stub" and run["finished_at"] is not None
    arts = _artifacts(api, run_id)
    assert len(arts) == 1 and arts[0]["kind"] == "doc" and arts[0]["storage_ref"].startswith("fs:")
    assert _task_status(api, tid) == "in_review"  # success -> submit_review


def test_timeout_retries_twice_then_dead_letters(api: dict[str, Any], tmp_path: Any) -> None:
    _pid, _tid, agent, run_id = _setup_assigned_run(api)

    async def scenario() -> int:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamBus(redis)
        storage = FilesystemStorage(tmp_path)
        executor = _SlowExecutor()
        await bus.ensure_group(STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP)
        await bus.publish(
            STREAM_RUNS_EXECUTE, {"run_id": run_id, "task_id": _tid, "agent_id": agent}, actor="t"
        )
        # Attempt 1 (initial delivery) times out -> timed_out + nack.
        await coordinator.process_execute_batch(
            api["engine"], bus, storage, executor, "w1", concurrency=4, soft_timeout=0.05
        )
        # Reap (min-idle 0) retries until the QueueBus dead-letters past max attempts.
        for _ in range(6):
            await coordinator.reap_execute_once(
                api["engine"], bus, storage, executor, "w1", soft_timeout=0.05, min_idle_ms=0
            )
            if await redis.xlen(dlq_stream(STREAM_RUNS_EXECUTE)) > 0:
                break
        depth = await redis.xlen(dlq_stream(STREAM_RUNS_EXECUTE))
        await redis.aclose()
        return int(depth)

    dlq_depth = run_async(scenario)
    assert dlq_depth == 1  # dead-lettered after retries
    assert _run_row(api, run_id)["status"] == "timed_out"


def test_worker_killed_mid_run_is_reclaimed_without_duplicate(
    api: dict[str, Any], tmp_path: Any
) -> None:
    pid, tid, agent, run_id = _setup_assigned_run(api)

    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamBus(redis)
        storage = FilesystemStorage(tmp_path)
        await bus.ensure_group(STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP)
        await bus.publish(
            STREAM_RUNS_EXECUTE, {"run_id": run_id, "task_id": tid, "agent_id": agent}, actor="t"
        )
        # Worker A takes the run (marks it running) then "dies" — never finalizes
        # or acks, so the message stays pending and the run is stuck `running`.
        got = await bus.consume(
            STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP, "A", block_ms=100
        )
        assert len(got) == 1
        async with api["engine"].begin() as conn:
            assert await runs.mark_running(conn, uuid.UUID(run_id)) is not None
        # Worker B reclaims via XAUTOCLAIM and completes it.
        reclaimed = await coordinator.reap_execute_once(
            api["engine"], bus, storage, StubExecutor(), "B", soft_timeout=5.0, min_idle_ms=0
        )
        assert reclaimed == 1
        await redis.aclose()

    run_async(scenario)

    assert _run_row(api, run_id)["status"] == "succeeded"
    assert len(_artifacts(api, run_id)) == 1  # exactly one — no duplicate side effect


def test_dispatch_run_enqueues_through_the_outbox(api: dict[str, Any], tmp_path: Any) -> None:
    """The real enqueue seam: dispatch_run writes an outbox row → the drainer
    publishes it to stream:runs.execute → the executor runs it."""
    pid, tid, agent, run_id = _setup_assigned_run(api)

    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamBus(redis)
        storage = FilesystemStorage(tmp_path)
        await bus.ensure_group(STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP)
        async with api["engine"].begin() as conn:
            await coordinator.dispatch_run(
                conn,
                run_id=uuid.UUID(run_id),
                task_id=uuid.UUID(tid),
                agent_id=uuid.UUID(agent),
            )
        await OutboxDrainer(api["engine"], bus).drain_once()  # outbox -> stream
        processed = await coordinator.process_execute_batch(
            api["engine"], bus, storage, StubExecutor(), "w1", concurrency=4, soft_timeout=5.0
        )
        assert processed >= 1
        await redis.aclose()

    run_async(scenario)
    assert _run_row(api, run_id)["status"] == "succeeded"


def test_orphaned_result_is_reclaimed_and_drives_review(api: dict[str, Any], tmp_path: Any) -> None:
    """A results message whose consumer crashed (nacked) is reclaimed by the
    results reaper and reprocessed, so the succeeded run's task still reaches
    in_review instead of being stranded in in_progress."""
    pid, tid, agent, run_id = _setup_assigned_run(api)

    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamBus(redis)
        storage = FilesystemStorage(tmp_path)
        await bus.ensure_group(STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP)
        await bus.ensure_group(STREAM_RUNS_RESULTS, coordinator.RUNS_RESULTS_GROUP)
        await bus.publish(
            STREAM_RUNS_EXECUTE, {"run_id": run_id, "task_id": tid, "agent_id": agent}, actor="t"
        )
        await coordinator.process_execute_batch(
            api["engine"], bus, storage, StubExecutor(), "w1", concurrency=4, soft_timeout=5.0
        )
        await OutboxDrainer(api["engine"], bus).drain_once()  # result -> stream:runs.results
        # Consumer A takes the result then "crashes" (nack, no processing).
        got = await bus.consume(
            STREAM_RUNS_RESULTS, coordinator.RUNS_RESULTS_GROUP, "A", block_ms=100
        )
        assert len(got) == 1
        await bus.nack(STREAM_RUNS_RESULTS, coordinator.RUNS_RESULTS_GROUP, got[0])
        # The results reaper reclaims the orphan and drives the state machine.
        reclaimed = await coordinator.reap_results_once(api["engine"], bus, "B", min_idle_ms=0)
        assert reclaimed == 1
        await redis.aclose()

    run_async(scenario)
    assert _task_status(api, tid) == "in_review"  # not stranded in in_progress
