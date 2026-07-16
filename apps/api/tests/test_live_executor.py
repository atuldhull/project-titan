"""LiveExecutor — the real agent turn as the delivery run executor (Phase 56).

Schema-marked; deterministic via the ScriptedLlm runtime (no Ollama). Proves the
RunContext → run_turn → RunOutcome mapping: a completed turn → succeeded, a
non-completed turn → failed (recorded, not massaged), an unmapped agent → failed,
and that a build_log artifact always bridges the metadata-only TurnResult."""

import uuid
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text

from tests.conftest import run_async
from tests.test_runtime_turn import ScriptedLlm, _make_project, _report, _resp, _turn_json
from titan.db import uuid7
from titan.runs.executor import RunContext
from titan.runs.live_executor import LiveExecutor, make_role_resolver
from titan.runtime.service import build_runtime
from titan.settings import Settings

pytestmark = pytest.mark.schema


def _runtime(engine: Any, script: dict[str, list[Any]]) -> Any:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    runtime = build_runtime(Settings(), engine, redis=fake, llm=ScriptedLlm(script))
    runtime.backoff_base_s = 0.0
    return runtime


async def _seed_task_and_agent(
    engine: Any, pid: uuid.UUID, *, role: str
) -> tuple[uuid.UUID, uuid.UUID]:
    tid, aid = uuid7(), uuid7()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tasks (id, project_id, title, spec) VALUES (:id, :p, :t, :s)"),
            {"id": tid, "p": pid, "t": "create-task endpoint", "s": "Implement POST /tasks"},
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, project_id, role, name, model_binding) "
                "VALUES (:id, :p, :r, :n, CAST(:mb AS jsonb))"
            ),
            {"id": aid, "p": pid, "r": role, "n": f"{role}-bot", "mb": "{}"},
        )
    return tid, aid


def _ctx(pid: uuid.UUID, tid: uuid.UUID, aid: uuid.UUID) -> RunContext:
    return RunContext(
        run_id=uuid7(),
        task_id=tid,
        agent_id=aid,
        project_id=pid,
        attempt=1,
        spec="Implement the create-task endpoint",
        input={},
    )


def test_completed_turn_maps_to_succeeded(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        tid, aid = await _seed_task_and_agent(migrated_engine, pid, role="backend")
        runtime = _runtime(
            migrated_engine, {"agent.eng_backend": [_resp(text=_turn_json([_report("done")]))]}
        )
        executor = LiveExecutor(runtime, make_role_resolver(migrated_engine))
        outcome = await executor.execute(_ctx(pid, tid, aid))
        assert outcome.status == "succeeded"
        assert outcome.error is None
        assert float(outcome.cost_usd) == 0.0  # Ollama is metered free
        assert any(a.kind == "build_log" for a in outcome.artifacts)

    run_async(scenario)


def test_failed_turn_maps_to_failed(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        tid, aid = await _seed_task_and_agent(migrated_engine, pid, role="backend")
        # Three invalid outputs exhaust the self-critique ladder → not completed.
        runtime = _runtime(
            migrated_engine, {"agent.eng_backend": [_resp(text="nope") for _ in range(3)]}
        )
        executor = LiveExecutor(runtime, make_role_resolver(migrated_engine))
        outcome = await executor.execute(_ctx(pid, tid, aid))
        assert outcome.status == "failed"
        assert outcome.error
        assert any(a.kind == "build_log" for a in outcome.artifacts)

    run_async(scenario)


def test_unmapped_agent_fails_without_a_turn(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)

        async def resolve(_agent_id: uuid.UUID) -> str | None:
            return None

        executor = LiveExecutor(_runtime(migrated_engine, {}), resolve)
        outcome = await executor.execute(_ctx(pid, uuid7(), uuid7()))
        assert outcome.status == "failed" and outcome.error is not None
        assert "roster role" in outcome.error

    run_async(scenario)


def test_role_resolver_maps_agent_table_role(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        _, aid = await _seed_task_and_agent(migrated_engine, pid, role="db")
        resolve = make_role_resolver(migrated_engine)
        assert await resolve(aid) == "eng_database"
        assert await resolve(uuid7()) is None  # no such agent

    run_async(scenario)


def test_live_executor_drives_a_run_through_the_coordinator(api: Any, tmp_path: Any) -> None:
    """The Phase-56 seam end-to-end: the coordinator pops a run, invokes the
    LiveExecutor (a REAL turn via ScriptedLlm), and persists the outcome — the run
    finalizes succeeded with the turn's provider + a build_log artifact."""
    from tests.test_run_lifecycle import _artifacts, _run_row, _setup_assigned_run
    from titan.events.outbox import OutboxDrainer
    from titan.events.redis_bus import RedisStreamBus
    from titan.events.service import STREAM_RUNS_EXECUTE, STREAM_RUNS_RESULTS
    from titan.runs import coordinator
    from titan.runs.storage import FilesystemStorage

    _pid, tid, agent, run_id = _setup_assigned_run(api)  # agent role defaults to "backend"

    async def scenario() -> None:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamBus(redis)
        runtime = build_runtime(
            Settings(),
            api["engine"],
            redis=redis,
            llm=ScriptedLlm({"agent.eng_backend": [_resp(text=_turn_json([_report("done")]))]}),
        )
        runtime.backoff_base_s = 0.0
        executor = LiveExecutor(runtime, make_role_resolver(api["engine"]))
        await bus.ensure_group(STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP)
        await bus.ensure_group(STREAM_RUNS_RESULTS, coordinator.RUNS_RESULTS_GROUP)
        await bus.publish(
            STREAM_RUNS_EXECUTE,
            {"run_id": run_id, "task_id": tid, "agent_id": agent},
            actor="test",
        )
        processed = await coordinator.process_execute_batch(
            api["engine"],
            bus,
            FilesystemStorage(tmp_path),
            executor,
            "w1",
            concurrency=4,
            soft_timeout=30.0,
        )
        assert processed == 1
        await OutboxDrainer(api["engine"], bus).drain_once()
        await coordinator.process_results_batch(api["engine"], bus, "w1")
        await redis.aclose()

    run_async(scenario)

    run = _run_row(api, run_id)
    assert run["status"] == "succeeded"
    assert run["provider"] and run["provider"] != "stub"  # a real turn, not the placeholder
    assert any(a["kind"] == "build_log" for a in _artifacts(api, run_id))
