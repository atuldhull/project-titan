"""Rebuild the run-execution queue from the PostgreSQL ledger after queue loss
(Vol 9 §8.2 / §9, TITAN-OPS-020).

Redis Streams are deliberately NOT the durability boundary: every run is recorded
in the ``runs`` ledger before enqueue, so a flushed/lost ``stream:runs.execute``
is fully recoverable by re-publishing every non-terminal run — queued, running,
OR timed_out (``mark_running`` re-takes all three on re-delivery; only
succeeded/failed/cancelled are terminal, so a timed_out run left mid retry-window
by a queue wipe would otherwise be orphaned forever).

PRECONDITION (RUNBOOK-04 step 4 — "start Redis empty, run ledger re-enqueue"):
run this only AFTER the execution tier is stopped/restarted. Re-enqueueing a run
whose worker is STILL executing it would double-execute that run — ``mark_running``
re-takes a 'running' row, and the single-active-owner index bounds runs-PER-TASK,
not executions-per-run. In the intended DR flow (queue lost, workers restarted)
nothing is mid-flight, so recovery is clean and idempotent (a peer that finalized
a run first wins the status-guarded writes).

The publishing bus is SIGNED with the same envelope key the worker verifies with
(TITAN-SEC-020); an unsigned requeue would be dropped + audited as a forged
frame, silently recovering nothing.

Usage (from the API service env, workers stopped): uv run --env-file ../../.env \
    python scripts/requeue_from_ledger.py
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan import clients
from titan.events import service as events
from titan.events.service import STREAM_RUNS_EXECUTE, QueueBus
from titan.observability.logging import configure_logging
from titan.settings import get_settings
from titan.vault import bootstrap as vault_bootstrap

# Runs that have NOT reached a terminal state — the work still owed. mark_running
# re-takes exactly these on re-delivery (terminal = succeeded/failed/cancelled).
# timed_out MUST be here: a queue wipe during the timeout→retry window would
# otherwise strand it (the reaper can't reclaim a wiped message).
_NON_TERMINAL: tuple[str, ...] = ("queued", "running", "timed_out")


@dataclass
class RequeueReport:
    scanned: int
    requeued: int


async def requeue_from_ledger(engine: AsyncEngine, bus: QueueBus) -> RequeueReport:
    """Re-publish every non-terminal run to stream:runs.execute. Returns counts.
    Directly XADDs through ``bus`` (not the outbox — those rows are already
    published, so the drainer would not re-emit them after a queue wipe)."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, task_id, agent_id FROM runs "
                    "WHERE status = ANY(:states) ORDER BY created_at"
                ),
                {"states": list(_NON_TERMINAL)},
            )
        ).all()
    requeued = 0
    for row in rows:
        await bus.publish(
            STREAM_RUNS_EXECUTE,
            {
                "run_id": str(row.id),
                "task_id": str(row.task_id),
                "agent_id": str(row.agent_id),
            },
            actor="requeue-from-ledger",
        )
        requeued += 1
    return RequeueReport(scanned=len(rows), requeued=requeued)


async def _run() -> RequeueReport:
    settings = get_settings()
    engine = clients.create_engine(settings)
    redis = clients.create_redis(settings)
    signer = vault_bootstrap.build_envelope_signer(settings)
    bus = events.build_bus(redis, signer=signer, maxlen=settings.stream_maxlen)
    try:
        return await requeue_from_ledger(engine, bus)
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    configure_logging(get_settings().log_level, service="titan-requeue")
    report = asyncio.run(_run())
    sys.stdout.write(
        f"requeue-from-ledger: scanned {report.scanned} non-terminal run(s), "
        f"re-enqueued {report.requeued} to {STREAM_RUNS_EXECUTE}\n"
    )


if __name__ == "__main__":
    main()
