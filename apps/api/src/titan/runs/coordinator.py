"""The worker's run coordinator (Vol 3 §8.2, TITAN-BCK-016/017).

Consumes ``stream:runs.execute`` through the QueueBus, drives each run's
lifecycle — running → execute (bounded soft timeout) → artifacts + token/cost
accounting → succeeded|failed — and publishes the outcome to
``stream:runs.results`` THROUGH the transactional outbox (never a raw XADD). A
second consumer drains ``stream:runs.results`` and drives the Phase-13 state
machine (success → submit_review).

Idempotency (TITAN-BCK-016 'no duplicate side effects'): every DB write is
status-guarded, so a reclaimed in-flight or timed-out run is retried while a
finalized one is skipped, and exactly one worker records a run's artifacts +
result. Retries and dead-lettering are the QueueBus's job (XAUTOCLAIM reaper +
DLQ after max attempts) — the coordinator only nacks a timed-out run.
"""

import asyncio
import contextlib
import uuid
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.events.bus import Delivery, QueueBus
from titan.events.service import STREAM_RUNS_EXECUTE, STREAM_RUNS_RESULTS, dlq_stream, write_event
from titan.observability.metrics import STREAM_DEPTH
from titan.orchestrator import service as orchestrator
from titan.runs import service as runs
from titan.runs.executor import RunContext, RunExecutor, RunOutcome
from titan.runs.storage import Storage

logger = structlog.get_logger("titan.runs")

RUNS_EXECUTE_GROUP = "titan-runs-execute"
RUNS_RESULTS_GROUP = "titan-runs-results"


async def dispatch_run(
    conn: AsyncConnection, *, run_id: uuid.UUID, task_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    """Enqueue a queued run for execution — an outbox row on stream:runs.execute,
    committed with the caller's transaction (the drainer publishes it)."""
    await write_event(
        conn,
        STREAM_RUNS_EXECUTE,
        {"run_id": str(run_id), "task_id": str(task_id), "agent_id": str(agent_id)},
        actor="scheduler",
    )


async def _resolve_context(engine: AsyncEngine, run: runs.RunOut) -> RunContext:
    """Resolve execution context (§8.2: task spec + input refs)."""
    async with engine.connect() as conn:
        task = await orchestrator.get_task(conn, run.task_id)
    return RunContext(
        run_id=run.id,
        task_id=run.task_id,
        agent_id=run.agent_id,
        project_id=task.project_id,
        attempt=run.attempt,
        spec=task.spec,
        input=run.input,
    )


def _failure_outcome(error: str) -> RunOutcome:
    return RunOutcome(
        status="failed",
        output_summary="run failed",
        provider="unknown",
        model="unknown",
        tokens_in=0,
        tokens_out=0,
        cost_usd=Decimal("0"),
        error=error,
    )


async def execute_one(
    engine: AsyncEngine,
    bus: QueueBus,
    storage: Storage,
    executor: RunExecutor,
    delivery: Delivery,
    *,
    soft_timeout: float,
) -> None:
    """Execute a single run-execute delivery through its full lifecycle."""
    try:
        run_id = uuid.UUID(delivery.envelope.payload["run_id"])
    except (KeyError, ValueError, TypeError):
        # A malformed message can never be processed — drop it (ack) rather than
        # let it abort the batch or retry forever.
        logger.error("run.malformed_execute_message", handle=delivery.handle)
        await bus.ack(STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP, delivery)
        return
    try:
        # Take (or re-take) the run + start its task. mark_running is the gate:
        # None => missing or terminal (already finalized) => ack, no side effect.
        async with engine.begin() as conn:
            started = await runs.mark_running(conn, run_id)
            if started is not None:
                await orchestrator.start_run_task(conn, started.task_id, started.agent_id)
        if started is None:
            await bus.ack(STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP, delivery)
            return

        context = await _resolve_context(engine, started)
        try:
            outcome = await asyncio.wait_for(executor.execute(context), timeout=soft_timeout)
        except TimeoutError:
            async with engine.begin() as conn:
                await runs.mark_timed_out(conn, run_id)
            logger.warning("run.timed_out", run_id=str(run_id), attempt=started.attempt)
            # Leave pending: the reaper retries, then dead-letters after max attempts.
            await bus.nack(STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP, delivery)
            return
        except Exception as exc:  # a hard executor error is a terminal `failed`
            logger.exception("run.executor_failed", run_id=str(run_id))
            outcome = _failure_outcome(str(exc))

        await _finalize(engine, storage, run_id, started, context, outcome)
        await bus.ack(STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP, delivery)
    except Exception:
        # Unexpected (DB blip, etc.): leave pending for the reaper to retry.
        logger.exception("run.execute_error", run_id=str(run_id))
        await bus.nack(STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP, delivery)


async def _finalize(
    engine: AsyncEngine,
    storage: Storage,
    run_id: uuid.UUID,
    run: runs.RunOut,
    context: RunContext,
    outcome: RunOutcome,
) -> None:
    """Record the terminal status + accounting, persist artifacts, and publish
    the result — all in ONE transaction, guarded so exactly one worker does it."""
    async with engine.begin() as conn:
        finalized = await runs.finalize_run(
            conn,
            run_id,
            status=outcome.status,
            output_summary=outcome.output_summary,
            error=outcome.error,
            provider=outcome.provider,
            model=outcome.model,
            tokens_in=outcome.tokens_in,
            tokens_out=outcome.tokens_out,
            cost_usd=outcome.cost_usd,
        )
        if finalized is None:
            return  # a peer already finalized — no duplicate artifacts/result
        for artifact in outcome.artifacts:
            await runs.record_artifact(
                conn,
                storage,
                run_id=run_id,
                project_id=context.project_id,
                kind=artifact.kind,
                data=artifact.data,
                path=artifact.path,
            )
        await write_event(
            conn,
            STREAM_RUNS_RESULTS,
            {
                "run_id": str(run_id),
                "task_id": str(run.task_id),
                "agent_id": str(run.agent_id),
                "succeeded": outcome.status == "succeeded",
            },
            actor="worker",
        )


async def process_execute_batch(
    engine: AsyncEngine,
    bus: QueueBus,
    storage: Storage,
    executor: RunExecutor,
    consumer: str,
    *,
    concurrency: int,
    soft_timeout: float,
    block_ms: int = 1000,
) -> int:
    """Consume up to ``concurrency`` runs and execute them concurrently (distinct
    agents run in parallel within one worker, TITAN-BCK-017)."""
    deliveries = await bus.consume(
        STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP, consumer, count=concurrency, block_ms=block_ms
    )
    if deliveries:
        await asyncio.gather(
            *(
                execute_one(engine, bus, storage, executor, d, soft_timeout=soft_timeout)
                for d in deliveries
            )
        )
    return len(deliveries)


async def reap_execute_once(
    engine: AsyncEngine,
    bus: QueueBus,
    storage: Storage,
    executor: RunExecutor,
    consumer: str,
    *,
    soft_timeout: float,
    min_idle_ms: int = 60_000,
) -> int:
    """Reclaim idle-pending runs from dead consumers and RE-EXECUTE them (retry);
    the QueueBus dead-letters any past max attempts. Publishes pending depth."""
    reclaimed = await bus.reap(
        STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP, consumer, min_idle_ms=min_idle_ms
    )
    for delivery in reclaimed:
        await execute_one(engine, bus, storage, executor, delivery, soft_timeout=soft_timeout)
    STREAM_DEPTH.labels(stream=STREAM_RUNS_EXECUTE, group=RUNS_EXECUTE_GROUP).set(
        await bus.pending_depth(STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP)
    )
    return len(reclaimed)


async def handle_result_delivery(engine: AsyncEngine, bus: QueueBus, delivery: Delivery) -> None:
    """Drive the state machine from one runs.results message (§8.2)."""
    payload = delivery.envelope.payload
    try:
        async with engine.begin() as conn:
            await orchestrator.handle_run_result(
                conn,
                task_id=uuid.UUID(payload["task_id"]),
                agent_id=uuid.UUID(payload["agent_id"]),
                succeeded=bool(payload["succeeded"]),
            )
        await bus.ack(STREAM_RUNS_RESULTS, RUNS_RESULTS_GROUP, delivery)
    except Exception:
        logger.exception("run.result_error", run_id=payload.get("run_id"))
        await bus.nack(STREAM_RUNS_RESULTS, RUNS_RESULTS_GROUP, delivery)


async def process_results_batch(
    engine: AsyncEngine, bus: QueueBus, consumer: str, *, block_ms: int = 1000
) -> int:
    deliveries = await bus.consume(
        STREAM_RUNS_RESULTS, RUNS_RESULTS_GROUP, consumer, count=50, block_ms=block_ms
    )
    for delivery in deliveries:
        await handle_result_delivery(engine, bus, delivery)
    return len(deliveries)


async def reap_results_once(
    engine: AsyncEngine, bus: QueueBus, consumer: str, *, min_idle_ms: int = 60_000
) -> int:
    """Reclaim idle-pending result deliveries (a crashed/nacked result) and
    reprocess them — without this a succeeded run's task never leaves in_progress.
    handle_run_result is idempotent, so reprocessing is safe; the QueueBus
    dead-letters any past max attempts."""
    reclaimed = await bus.reap(
        STREAM_RUNS_RESULTS, RUNS_RESULTS_GROUP, consumer, min_idle_ms=min_idle_ms
    )
    for delivery in reclaimed:
        await handle_result_delivery(engine, bus, delivery)
    STREAM_DEPTH.labels(stream=STREAM_RUNS_RESULTS, group=RUNS_RESULTS_GROUP).set(
        await bus.pending_depth(STREAM_RUNS_RESULTS, RUNS_RESULTS_GROUP)
    )
    return len(reclaimed)


# The run_* loops are long-running orchestration whose per-iteration units above
# are unit-tested; the loops are verified end-to-end by the live Compose worker.
# Each swallows transient errors so it self-heals — hence pragma: no cover.


async def _execute_slot(  # pragma: no cover
    engine: AsyncEngine,
    bus: QueueBus,
    storage: Storage,
    executor: RunExecutor,
    consumer: str,
    *,
    stop: asyncio.Event,
    soft_timeout: float,
) -> None:
    """One independent execution slot: pull ONE run, execute it, repeat. A slow
    run only occupies its own slot, so the worker keeps refilling the others
    (no batch barrier stalling intake)."""
    while not stop.is_set():
        try:
            deliveries = await bus.consume(
                STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP, consumer, count=1, block_ms=1000
            )
        except Exception:
            logger.exception("run.executor_slot_error")
            await _sleep_or_stop(stop, 2.0)
            continue
        if deliveries:
            await execute_one(
                engine, bus, storage, executor, deliveries[0], soft_timeout=soft_timeout
            )
        else:
            await _sleep_or_stop(stop, 0.1)


async def run_executor_loop(  # pragma: no cover
    engine: AsyncEngine,
    bus: QueueBus,
    storage: Storage,
    executor: RunExecutor,
    consumer: str,
    *,
    stop: asyncio.Event,
    concurrency: int,
    soft_timeout: float,
) -> None:
    """Run ``concurrency`` independent execution slots, each with its own consumer
    name, so up to N runs (distinct agents) are in flight at once (BCK-017)."""
    await bus.ensure_group(STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP)
    slots = [
        asyncio.create_task(
            _execute_slot(
                engine,
                bus,
                storage,
                executor,
                f"{consumer}-slot{i}",
                stop=stop,
                soft_timeout=soft_timeout,
            )
        )
        for i in range(concurrency)
    ]
    await asyncio.gather(*slots, return_exceptions=True)


async def run_execute_reaper(  # pragma: no cover
    engine: AsyncEngine,
    bus: QueueBus,
    storage: Storage,
    executor: RunExecutor,
    consumer: str,
    *,
    stop: asyncio.Event,
    soft_timeout: float,
    min_idle_ms: int,
) -> None:
    """Reclaim runs from GENUINELY dead consumers only. ``min_idle_ms`` must
    exceed the longest a live worker can hold a message (>= the soft timeout),
    or the reaper would reclaim and double-execute healthy long-running runs."""
    await bus.ensure_group(STREAM_RUNS_EXECUTE, RUNS_EXECUTE_GROUP)
    while not stop.is_set():
        try:
            await reap_execute_once(
                engine,
                bus,
                storage,
                executor,
                consumer,
                soft_timeout=soft_timeout,
                min_idle_ms=min_idle_ms,
            )
        except Exception:
            logger.exception("run.reaper_loop_error")
        await _sleep_or_stop(stop, 30.0)


async def run_results_loop(  # pragma: no cover
    engine: AsyncEngine, bus: QueueBus, consumer: str, *, stop: asyncio.Event
) -> None:
    await bus.ensure_group(STREAM_RUNS_RESULTS, RUNS_RESULTS_GROUP)
    while not stop.is_set():
        try:
            processed = await process_results_batch(engine, bus, consumer)
        except Exception:
            logger.exception("run.results_loop_error")
            await _sleep_or_stop(stop, 2.0)
            continue
        if not processed:
            await _sleep_or_stop(stop, 0.5)


async def run_results_reaper(  # pragma: no cover
    engine: AsyncEngine, bus: QueueBus, consumer: str, *, stop: asyncio.Event
) -> None:
    """Reclaim orphaned result deliveries so a nacked/crashed result still drives
    the state machine (a succeeded run is never left stuck in in_progress)."""
    await bus.ensure_group(STREAM_RUNS_RESULTS, RUNS_RESULTS_GROUP)
    while not stop.is_set():
        try:
            await reap_results_once(engine, bus, consumer)
        except Exception:
            logger.exception("run.results_reaper_loop_error")
        await _sleep_or_stop(stop, 30.0)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:  # pragma: no cover
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


__all__ = [
    "RUNS_EXECUTE_GROUP",
    "RUNS_RESULTS_GROUP",
    "dispatch_run",
    "dlq_stream",
    "execute_one",
    "process_execute_batch",
    "process_results_batch",
    "reap_execute_once",
    "reap_results_once",
    "run_execute_reaper",
    "run_executor_loop",
    "run_results_loop",
    "run_results_reaper",
]
