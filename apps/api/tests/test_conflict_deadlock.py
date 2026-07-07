"""Rule-5 deadlock detection + live thresholds (Vol 2 §7.5, TITAN-AGT-020) — needs
the disposable test DB.

Covers: a conflict reaching the round threshold and a task reopened past the reopen
threshold both surface a deadlock signal; the detection hook auto-escalates one
level and flags for the dashboard; and a threshold change takes effect on the next
detection with no redeploy."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.conflict import service as conflict

pytestmark = pytest.mark.schema


async def _make_project(engine: Any) -> uuid.UUID:
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'creator', 'local', :s)"
            ),
            {"id": uid, "e": f"u-{uid.hex[:12]}@t.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :n, :d, 'b', :u)"
            ),
            {"id": pid, "n": f"p-{pid.hex[:12]}", "d": "P", "u": uid},
        )
    return pid


async def _make_task(engine: Any, pid: uuid.UUID) -> uuid.UUID:
    tid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tasks (id, project_id, title) VALUES (:id, :p, 'work')"),
            {"id": tid, "p": pid},
        )
    return tid


async def _write_reopen(engine: Any, pid: uuid.UUID, tid: uuid.UUID) -> None:
    """Mimic the orchestrator's reopen transition message (body action=reopen)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO messages (id, project_id, thread_id, kind, body, task_id, from_agent) "
                "VALUES (:id, :p, :thr, 'bug_report', CAST(:body AS jsonb), :tid, 'pm_project')"
            ),
            {
                "id": uuid.uuid4(),
                "p": pid,
                "thr": uuid.uuid4(),
                "body": json.dumps({"action": "reopen", "from": "done", "to": "reopened"}),
                "tid": tid,
            },
        )


# --- round deadlock (a conflict stuck at a level) --------------------------


def test_round_deadlock_is_detected_and_escalates_one_level(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        cid = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="technical",
                subject="stuck",
                parties=["eng_backend", "eng_frontend"],
                opened_by="eng_backend",
            )
        # Two more direct rounds with no progress → round 3 (the default threshold).
        for _ in range(2):
            async with migrated_engine.begin() as conn:
                await conflict.record_round(conn, pid, cid, actor="eng_backend")

        async with migrated_engine.connect() as conn:
            signal = await conflict.detect_conflict_deadlock(conn, pid, cid)
        assert signal is not None
        assert signal.scope == "conflict_rounds"
        assert signal.current == 3 and signal.threshold == 3
        assert signal.escalate_to == "arbitration"

        # Applying the escalation moves one level up, appoints the arbiter, and flags.
        async with migrated_engine.begin() as conn:
            escalated = await conflict.apply_deadlock_escalation(conn, pid, cid)
        assert escalated.state == "arbitration"
        assert escalated.arbiter == "architect"
        assert escalated.deadlock_flagged is True
        assert escalated.round == 1  # fresh round budget at the new level

        # Crucially, detection does NOT immediately re-fire: the escalation reset the
        # round, so the newly-appointed arbiter gets a turn before it can deadlock
        # again — the rule moves exactly ONE level, never cascading past the arbiter.
        async with migrated_engine.connect() as conn:
            assert await conflict.detect_conflict_deadlock(conn, pid, cid) is None

    run_async(scenario)


def test_no_deadlock_below_threshold(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        cid = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="technical",
                subject="fine",
                parties=["a", "b"],
                opened_by="a",
            )
        async with migrated_engine.connect() as conn:
            assert await conflict.detect_conflict_deadlock(conn, pid, cid) is None

    run_async(scenario)


# --- reopen deadlock (a task reopened too many times) ----------------------


def test_reopen_deadlock_is_detected(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        tid = await _make_task(migrated_engine, pid)
        for _ in range(3):
            await _write_reopen(migrated_engine, pid, tid)
        async with migrated_engine.connect() as conn:
            signal = await conflict.detect_reopen_deadlock(conn, pid, tid)
        assert signal is not None
        assert signal.scope == "task_reopens" and signal.current == 3 and signal.threshold == 3
        assert signal.subject_id == tid

    run_async(scenario)


def test_two_reopens_is_not_yet_a_deadlock(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        tid = await _make_task(migrated_engine, pid)
        for _ in range(2):
            await _write_reopen(migrated_engine, pid, tid)
        async with migrated_engine.connect() as conn:
            assert await conflict.detect_reopen_deadlock(conn, pid, tid) is None

    run_async(scenario)


# --- live thresholds: change takes effect without redeploy (TITAN-AGT-020) --


def test_threshold_change_takes_effect_without_redeploy(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        cid = uuid.uuid4()
        async with migrated_engine.begin() as conn:
            await conflict.open_conflict(
                conn,
                pid,
                conflict_id=cid,
                kind="priority",
                subject="tighten",
                parties=["pm_project", "eng_backend"],
                opened_by="pm_project",
            )
            # One extra round → round 2. Under the default (3) this is NOT a deadlock.
            await conflict.record_round(conn, pid, cid, actor="pm_project")
        async with migrated_engine.connect() as conn:
            assert await conflict.detect_conflict_deadlock(conn, pid, cid) is None

        # Tighten the project threshold to 2 — no restart, no redeploy.
        async with migrated_engine.begin() as conn:
            await conflict.set_thresholds(
                conn, project_id=pid, max_conflict_rounds=2, max_task_reopens=3
            )
        # The SAME round-2 conflict is now deadlocked on the very next detection.
        async with migrated_engine.connect() as conn:
            signal = await conflict.detect_conflict_deadlock(conn, pid, cid)
        assert signal is not None and signal.threshold == 2 and signal.current == 2

        # Loosening it again clears the signal — the read is always fresh.
        async with migrated_engine.begin() as conn:
            await conflict.set_thresholds(
                conn, project_id=pid, max_conflict_rounds=5, max_task_reopens=5
            )
        async with migrated_engine.connect() as conn:
            assert await conflict.detect_conflict_deadlock(conn, pid, cid) is None

    run_async(scenario)


def test_project_threshold_overrides_global_default(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid = await _make_project(migrated_engine)
        # Set a global default AND a project override; the project value wins.
        async with migrated_engine.begin() as conn:
            await conflict.set_thresholds(
                conn, project_id=None, max_conflict_rounds=9, max_task_reopens=9
            )
            await conflict.set_thresholds(
                conn, project_id=pid, max_conflict_rounds=2, max_task_reopens=2
            )
        async with migrated_engine.connect() as conn:
            thresholds = await conflict.get_thresholds(conn, pid)
        assert thresholds.max_conflict_rounds == 2 and thresholds.max_task_reopens == 2

    run_async(scenario)
