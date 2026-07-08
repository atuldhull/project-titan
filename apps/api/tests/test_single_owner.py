"""The single-owner constraint (Vol 2 §5, TITAN-AGT-012) — needs the disposable
test DB.

"Every task has exactly one owner agent at any time", enforced in PostgreSQL
itself via the ``uq_runs_single_active_owner`` partial unique index: two
transactions racing to put two different agents on one task lose AT THE DB (a
unique violation raised by Postgres), not in application code."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import run_async

pytestmark = pytest.mark.schema


async def _seed(engine: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A project with one task and two agents."""
    pid, uid, tid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    agent_a, agent_b = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'c', 'local', :s)"
            ),
            {"id": uid, "e": f"u-{uid.hex[:12]}@t.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :n, 'P', 'b', :u)"
            ),
            {"id": pid, "n": f"p-{pid.hex[:12]}", "u": uid},
        )
        await conn.execute(
            text("INSERT INTO tasks (id, project_id, title) VALUES (:id, :p, 'contested')"),
            {"id": tid, "p": pid},
        )
        for agent_id, name in ((agent_a, "a"), (agent_b, "b")):
            await conn.execute(
                text(
                    "INSERT INTO agents (id, project_id, role, name, model_binding) "
                    "VALUES (:id, :p, 'backend', :n, '{}'::jsonb)"
                ),
                {"id": agent_id, "p": pid, "n": name},
            )
    return pid, tid, agent_a, agent_b


def test_concurrent_assignment_of_two_agents_fails_at_the_db(migrated_engine: Any) -> None:
    """The TITAN-AGT-012 acceptance: two transactions concurrently insert an
    ACTIVE run for the same task, each for a different agent. The second insert
    BLOCKS on the partial unique index until the first commits, then Postgres
    itself raises the unique violation — the loser fails at the DB, with no
    application-level guard involved (these are raw INSERTs)."""

    async def scenario() -> None:
        _pid, tid, agent_a, agent_b = await _seed(migrated_engine)
        started = asyncio.Event()

        async def claim(agent_id: uuid.UUID, attempt: int, *, hold: bool) -> str:
            try:
                async with migrated_engine.begin() as conn:
                    await conn.execute(
                        text(
                            "INSERT INTO runs (id, task_id, agent_id, attempt, input) "
                            "VALUES (:id, :tid, :aid, :att, '{}'::jsonb)"
                        ),
                        {"id": uuid.uuid4(), "tid": tid, "aid": agent_id, "att": attempt},
                    )
                    if hold:
                        started.set()  # the row is in; let the rival collide
                        await asyncio.sleep(0.3)  # hold the txn open across the race
                return "won"
            except IntegrityError as exc:
                assert "uq_runs_single_active_owner" in str(exc.orig)
                return "lost-at-db"

        async def rival() -> str:
            await started.wait()  # guarantee true overlap with the holder's txn
            return await claim(agent_b, 2, hold=False)

        outcomes = await asyncio.gather(claim(agent_a, 1, hold=True), rival())
        assert sorted(outcomes) == ["lost-at-db", "won"], outcomes

        # Exactly one active owner survived.
        async with migrated_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT agent_id FROM runs WHERE task_id = :tid "
                    "AND status IN ('queued', 'running')"
                ),
                {"tid": tid},
            )
            owners = result.all()
        assert len(owners) == 1 and owners[0].agent_id == agent_a

    run_async(scenario)


def test_second_owner_allowed_once_first_is_terminal(migrated_engine: Any) -> None:
    """Historical reassignment stays legal: once attempt 1 is terminal (failed),
    a DIFFERENT agent can take attempt 2 — the constraint binds only on ACTIVE
    ownership, exactly "one owner at any time"."""

    async def scenario() -> None:
        _pid, tid, agent_a, agent_b = await _seed(migrated_engine)
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, task_id, agent_id, attempt, input, status) "
                    "VALUES (:id, :tid, :aid, 1, '{}'::jsonb, 'failed')"
                ),
                {"id": uuid.uuid4(), "tid": tid, "aid": agent_a},
            )
            await conn.execute(  # attempt 2 by another agent — legal, prior is terminal
                text(
                    "INSERT INTO runs (id, task_id, agent_id, attempt, input) "
                    "VALUES (:id, :tid, :aid, 2, '{}'::jsonb)"
                ),
                {"id": uuid.uuid4(), "tid": tid, "aid": agent_b},
            )

    run_async(scenario)


def test_timed_out_revival_declined_when_a_newer_attempt_owns_the_task(
    migrated_engine: Any,
) -> None:
    """The execute-reaper revival path respects the single owner: reviving a
    timed_out run while a NEWER attempt is active would mint two owners, so
    mark_running declines (None → the coordinator acks and skips) instead of
    tripping the index mid-coordinator."""
    from titan.runs import service as runs_service

    async def scenario() -> None:
        _pid, tid, agent_a, agent_b = await _seed(migrated_engine)
        old_run = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, task_id, agent_id, attempt, input, status) "
                    "VALUES (:id, :tid, :aid, 1, '{}'::jsonb, 'timed_out')"
                ),
                {"id": old_run, "tid": tid, "aid": agent_a},
            )
            await conn.execute(  # the newer attempt now owns the task
                text(
                    "INSERT INTO runs (id, task_id, agent_id, attempt, input) "
                    "VALUES (:id, :tid, :aid, 2, '{}'::jsonb)"
                ),
                {"id": uuid.uuid4(), "tid": tid, "aid": agent_b},
            )
        async with migrated_engine.begin() as conn:
            revived = await runs_service.mark_running(conn, old_run)
        assert revived is None  # declined cleanly, no IntegrityError
        # Without a rival, the same revival succeeds (retry semantics intact).
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("UPDATE runs SET status = 'cancelled' WHERE task_id = :tid AND attempt = 2"),
                {"tid": tid},
            )
            revived = await runs_service.mark_running(conn, old_run)
        assert revived is not None and revived.status == "running"

    run_async(scenario)


def test_same_agent_cannot_hold_two_active_runs_either(migrated_engine: Any) -> None:
    """The constraint is per TASK, not per pair: even the SAME agent cannot open
    a second active run on a task (one owner, one active attempt)."""

    async def scenario() -> None:
        _pid, tid, agent_a, _agent_b = await _seed(migrated_engine)
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, task_id, agent_id, attempt, input) "
                    "VALUES (:id, :tid, :aid, 1, '{}'::jsonb)"
                ),
                {"id": uuid.uuid4(), "tid": tid, "aid": agent_a},
            )
        with pytest.raises(IntegrityError) as ei:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO runs (id, task_id, agent_id, attempt, input) "
                        "VALUES (:id, :tid, :aid, 2, '{}'::jsonb)"
                    ),
                    {"id": uuid.uuid4(), "tid": tid, "aid": agent_a},
                )
        assert "uq_runs_single_active_owner" in str(ei.value.orig)

    run_async(scenario)
