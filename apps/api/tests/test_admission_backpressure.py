"""Phase 72 (Vol 9 §9, TITAN-OPS-020) + kill switches (TITAN-OPS-012):

- per-project admission quota → 429 + Retry-After;
- the intake.read_only kill switch → 503 + Retry-After;
- the agents.pause_all dispatch gate (dispatch-rate → 0) + counter;
- oldest-pending backpressure metric;
- requeue-from-ledger recovery;
- the supervised-inbox pause gate.

Each control test is re-breakable: disabling the control flips the assertion."""

import uuid
from types import SimpleNamespace
from typing import Any, cast

import fakeredis.aioredis
import pytest
from prometheus_client import REGISTRY

from titan.events.redis_bus import RedisStreamBus
from titan.events.service import STREAM_RUNS_EXECUTE
from titan.runs import coordinator
from titan.runs import service as runs
from titan.runs.executor import StubExecutor
from titan.runs.storage import FilesystemStorage
from titan.runtime.turn import AgentRuntime
from titan.runtime.worker import RuntimeWorker

from .conftest import run_async

pytestmark = pytest.mark.schema

_MB = {"provider": "ollama", "model": "q"}


def _project(api: dict[str, Any]) -> str:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    return str(
        api["client"]
        .post(
            "/api/v1/projects",
            json={"name": name, "display_name": name, "brief": "b"},
            headers=api["token_for"]("owner"),
        )
        .json()["id"]
    )


def _agent(api: dict[str, Any]) -> str:
    return str(
        api["client"]
        .post(
            "/api/v1/agents",
            json={"role": "backend", "name": "backend-bot", "model_binding": _MB},
            headers=api["token_for"]("owner"),
        )
        .json()["id"]
    )


def _new_task(api: dict[str, Any], pid: str) -> Any:
    return api["client"].post(
        f"/api/v1/projects/{pid}/tasks",
        json={"title": "t", "spec": "s"},
        headers=api["token_for"]("owner"),
    )


# --- admission control -----------------------------------------------------


def test_per_project_quota_returns_429_with_retry_after(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app = api["client"].app
    monkeypatch.setattr(app.state.settings, "per_project_concurrent_task_quota", 2)
    pid = _project(api)
    # Two non-terminal tasks fill the quota.
    assert _new_task(api, pid).status_code == 201
    assert _new_task(api, pid).status_code == 201
    # The third is refused — but with 429 (retryable), NOT a 5xx, and a Retry-After.
    refused = _new_task(api, pid)
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) >= 1
    # Re-break: quota 0 disables the gate → the same request is accepted.
    monkeypatch.setattr(app.state.settings, "per_project_concurrent_task_quota", 0)
    assert _new_task(api, pid).status_code == 201
    # And a DIFFERENT project is unaffected by the first project's load.
    monkeypatch.setattr(app.state.settings, "per_project_concurrent_task_quota", 2)
    assert _new_task(api, _project(api)).status_code == 201


def test_intake_read_only_kill_switch_blocks_new_work(api: dict[str, Any]) -> None:
    pid = _project(api)
    assert _new_task(api, pid).status_code == 201  # accepted while OFF

    def set_read_only(on: bool) -> Any:
        return api["client"].put(
            "/api/v1/flags/intake.read_only",
            json={"enabled": on},
            headers=api["token_for"]("owner"),
        )

    assert set_read_only(True).status_code == 200
    try:
        blocked = _new_task(api, pid)
        assert blocked.status_code == 503
        assert int(blocked.headers["Retry-After"]) >= 1
    finally:
        # MUST reset — the flag + app store are shared across the session DB.
        assert set_read_only(False).status_code == 200
    assert _new_task(api, pid).status_code == 201  # accepted again once OFF


def test_intake_read_only_also_blocks_project_creation(api: dict[str, Any]) -> None:
    """read_only covers POST /projects too (the CEO auto-intake entry) — not just
    POST /tasks. Otherwise the CEO pipeline keeps generating work while 'read-only'."""

    def set_ro(on: bool) -> Any:
        return api["client"].put(
            "/api/v1/flags/intake.read_only",
            json={"enabled": on},
            headers=api["token_for"]("owner"),
        )

    assert set_ro(True).status_code == 200
    try:
        r = api["client"].post(
            "/api/v1/projects",
            json={"name": f"ro-{uuid.uuid4().hex[:8]}", "display_name": "ro", "brief": "b"},
            headers=api["token_for"]("owner"),
        )
        assert r.status_code == 503
        assert int(r.headers["Retry-After"]) >= 1
    finally:
        assert set_ro(False).status_code == 200


def test_intake_flag_read_failure_fails_open_no_5xx(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """OPS-020 'zero 5xx on intake': a flag-store read failure must degrade to
    ACCEPTING + ledgering (fail-open), never a 500. Re-break: use is_enabled (not
    is_enabled_or) in create_task and this returns 500."""
    pid = _project(api)  # created before we break the flag store
    app = api["client"].app

    async def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("flag store is down")

    monkeypatch.setattr(app.state.flags, "is_enabled", boom)
    assert _new_task(api, pid).status_code == 201


# --- dispatch kill switch (TITAN-OPS-012 acceptance) -----------------------


def _queued_run(api: dict[str, Any]) -> tuple[str, str, str]:
    pid = _project(api)
    agent = _agent(api)
    tid = str(_new_task(api, pid).json()["id"])
    api["client"].post(
        f"/api/v1/tasks/{tid}:transition",
        json={"action": "ready"},
        headers=api["token_for"]("owner"),
    )
    api["client"].post(
        f"/api/v1/tasks/{tid}:transition",
        json={"action": "assign", "actor_agent_id": agent},
        headers=api["token_for"]("owner"),
    )
    run_id = str(
        api["client"]
        .post(
            f"/api/v1/tasks/{tid}/runs",
            json={"agent_id": agent, "input": {"prompt_ref": "c"}},
            headers=api["token_for"]("owner"),
        )
        .json()["id"]
    )
    return tid, agent, run_id


def _dispatched_total() -> float:
    return REGISTRY.get_sample_value("titan_tasks_dispatched_total") or 0.0


def test_pause_all_halts_dispatch_and_flatlines_the_counter(
    api: dict[str, Any], tmp_path: Any
) -> None:
    tid, agent, run_id = _queued_run(api)

    async def paused() -> bool:
        return True

    async def running() -> bool:
        return False

    async def scenario() -> tuple[int, float, int, float]:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamBus(redis)
        storage = FilesystemStorage(tmp_path)
        await bus.ensure_group(STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP)
        await bus.publish(
            STREAM_RUNS_EXECUTE, {"run_id": run_id, "task_id": tid, "agent_id": agent}, actor="t"
        )
        before = _dispatched_total()
        # Paused: the cycle consumes NOTHING (message stays pending) and the
        # counter does not move — the dispatch-rate-to-zero acceptance signal.
        paused_n = await coordinator._dispatch_cycle(
            api["engine"],
            bus,
            storage,
            StubExecutor(),
            "w",
            count=4,
            soft_timeout=5.0,
            paused=paused,
        )
        paused_delta = _dispatched_total() - before
        # Unpaused: the SAME still-pending message now dispatches (proving pause
        # skipped consume, not just execution) and the counter advances.
        live_n = await coordinator._dispatch_cycle(
            api["engine"],
            bus,
            storage,
            StubExecutor(),
            "w",
            count=4,
            soft_timeout=5.0,
            paused=running,
        )
        live_delta = _dispatched_total() - before
        await redis.aclose()
        return paused_n, paused_delta, live_n, live_delta

    paused_n, paused_delta, live_n, live_delta = run_async(scenario)
    assert paused_n == 0 and paused_delta == 0.0  # halted, counter flat
    assert live_n == 1 and live_delta == 1.0  # resumed, counter moved


# --- oldest-pending backpressure metric ------------------------------------


def test_oldest_pending_age_seconds(tmp_path: Any) -> None:
    async def scenario() -> tuple[float, float, float]:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamBus(redis)
        group = "g"
        # No group yet → 0.0 (NOGROUP tolerated).
        nogroup = await bus.oldest_pending_age_seconds(STREAM_RUNS_EXECUTE, group)
        await bus.ensure_group(STREAM_RUNS_EXECUTE, group)
        empty = await bus.oldest_pending_age_seconds(STREAM_RUNS_EXECUTE, group)
        await bus.publish(STREAM_RUNS_EXECUTE, {"x": "1"}, actor="t")
        # Consume WITHOUT ack → the entry is now pending, so its age is measurable.
        await bus.consume(STREAM_RUNS_EXECUTE, group, "c", count=1, block_ms=100)
        pending = await bus.oldest_pending_age_seconds(STREAM_RUNS_EXECUTE, group)
        await redis.aclose()
        return nogroup, empty, pending

    nogroup, empty, pending = run_async(scenario)
    assert nogroup == 0.0 and empty == 0.0
    assert pending >= 0.0  # a real, non-negative age for the unacked entry


# --- requeue-from-ledger recovery ------------------------------------------


def test_requeue_restores_non_terminal_runs_including_timed_out(api: dict[str, Any]) -> None:
    """Recovery must re-enqueue queued, running AND timed_out runs (all re-taken by
    mark_running) but NOT terminal ones. A timed_out run left by a queue wipe would
    otherwise be orphaned forever — re-break: drop 'timed_out' from _NON_TERMINAL
    and this test's recovered-set assertion fails."""
    from decimal import Decimal

    from scripts.requeue_from_ledger import requeue_from_ledger

    _, _, queued_run = _queued_run(api)
    _, _, timed_out_run = _queued_run(api)  # -> running -> timed_out (non-terminal)
    _, _, done_run = _queued_run(api)  # -> succeeded (terminal)

    async def drive_states() -> None:
        async with api["engine"].begin() as conn:
            await runs.mark_running(conn, uuid.UUID(timed_out_run))
            await runs.mark_timed_out(conn, uuid.UUID(timed_out_run))
            await runs.mark_running(conn, uuid.UUID(done_run))
            await runs.finalize_run(
                conn,
                uuid.UUID(done_run),
                status="succeeded",
                output_summary="ok",
                error=None,
                provider="stub",
                model="m",
                tokens_in=1,
                tokens_out=1,
                cost_usd=Decimal("0"),
            )

    run_async(drive_states)

    async def scenario() -> tuple[Any, set[str]]:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamBus(redis)  # a fresh (empty) stream = the "flushed queue"
        report = await requeue_from_ledger(api["engine"], bus)
        # Read back exactly which run_ids were re-published.
        await bus.ensure_group(STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP)
        deliveries = await bus.consume(
            STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP, "c", count=100, block_ms=100
        )
        run_ids = {str(d.envelope.payload["run_id"]) for d in deliveries}
        await redis.aclose()
        return report, run_ids

    report, run_ids = run_async(scenario)
    # queued + timed_out recovered; the terminal (succeeded) run is NOT.
    assert queued_run in run_ids
    assert timed_out_run in run_ids  # the fix — timed_out is non-terminal + re-takeable
    assert done_run not in run_ids
    assert report.requeued == len(run_ids)


# --- supervised-inbox pause gate -------------------------------------------


class _FakeFlags:
    def __init__(self, paused: bool) -> None:
        self.paused = paused

    async def is_enabled(self, key: str, subject: str | None = None) -> bool:
        return self.paused


def test_process_available_consumes_nothing_when_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def fake_consume_inbox(*_a: Any, **_k: Any) -> list[Any]:
        calls.append(1)
        return []

    monkeypatch.setattr("titan.runtime.worker.consume_inbox", fake_consume_inbox)

    def worker(paused: bool) -> RuntimeWorker:
        runtime = cast(AgentRuntime, SimpleNamespace(flags=_FakeFlags(paused)))
        return RuntimeWorker(
            runtime=runtime, bus=cast(Any, None), project_id=uuid.uuid4(), agent_id="a"
        )

    # Paused: process_available short-circuits BEFORE consuming the inbox.
    assert run_async(lambda: worker(True).process_available()) == []
    assert calls == []
    # Re-break: not paused → it reaches consume_inbox (the gate is what stops it).
    run_async(lambda: worker(False).process_available())
    assert calls == [1]


def test_process_reclaimed_consumes_nothing_when_paused(monkeypatch: pytest.MonkeyPatch) -> None:
    """pause_all must halt the crash-recovery reclaim path too — else abandoned
    inbox messages get reprocessed as full agent work while the switch is ON."""
    calls: list[int] = []

    async def fake_reclaim(*_a: Any, **_k: Any) -> list[Any]:
        calls.append(1)
        return []

    monkeypatch.setattr("titan.runtime.worker.reclaim_unacked", fake_reclaim)

    def worker(paused: bool) -> RuntimeWorker:
        runtime = cast(AgentRuntime, SimpleNamespace(flags=_FakeFlags(paused)))
        return RuntimeWorker(
            runtime=runtime, bus=cast(Any, None), project_id=uuid.uuid4(), agent_id="a"
        )

    assert run_async(lambda: worker(True).process_reclaimed()) == []
    assert calls == []
    run_async(lambda: worker(False).process_reclaimed())
    assert calls == [1]


def test_pause_all_halts_the_execute_reaper_too(api: dict[str, Any], tmp_path: Any) -> None:
    """The reaper RE-EXECUTES reclaimed/timed-out runs — pause_all must halt that
    too, not just NEW dispatch, or the kill switch leaks full agent work through
    the retry path. Re-break: drop the paused gate in reap_execute_once and the
    paused run gets reclaimed+re-executed (paused_n becomes 1)."""
    tid, agent, run_id = _queued_run(api)

    async def paused() -> bool:
        return True

    async def running() -> bool:
        return False

    async def scenario() -> tuple[int, int]:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamBus(redis)
        storage = FilesystemStorage(tmp_path)
        await bus.ensure_group(STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP)
        await bus.publish(
            STREAM_RUNS_EXECUTE, {"run_id": run_id, "task_id": tid, "agent_id": agent}, actor="t"
        )
        # A now-"dead" consumer takes it -> pending, immediately reclaimable (min_idle_ms=0).
        await bus.consume(
            STREAM_RUNS_EXECUTE, coordinator.RUNS_EXECUTE_GROUP, "dead", count=1, block_ms=100
        )
        paused_n = await coordinator.reap_execute_once(
            api["engine"],
            bus,
            storage,
            StubExecutor(),
            "reaper",
            soft_timeout=5.0,
            min_idle_ms=0,
            paused=paused,
        )
        live_n = await coordinator.reap_execute_once(
            api["engine"],
            bus,
            storage,
            StubExecutor(),
            "reaper",
            soft_timeout=5.0,
            min_idle_ms=0,
            paused=running,
        )
        await redis.aclose()
        return paused_n, live_n

    paused_n, live_n = run_async(scenario)
    assert paused_n == 0  # halted — nothing reclaimed/re-executed
    assert live_n == 1  # resumed — reclaimed + re-executed
