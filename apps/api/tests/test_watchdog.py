"""The Orchestrator watchdog (Vol 2 §4.2 TITAN-AGT-009, §6.3, §5.4 TITAN-AGT-014,
§10 R6) — needs the disposable test DB + fakeredis.

Each Phase-32 behavior has its own test: (a) a killed consumer is replaced and the
replacement resumes with ZERO duplicate sends; (b) a requires_ack message past its
type-specific timeout re-enters the pending set via the existing hook and escalates;
(c) repeated flare-ups of one dispute (conflict_id/thread_id) produce exactly one
escalation; (d) a discussion past its decision deadline is escalated to the
moderator's supervisor and auto-closed with a DecisionLog. Thresholds are live
config (TITAN-AGT-020)."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.events.outbox import OutboxDrainer
from titan.events.redis_bus import RedisStreamBus
from titan.llm.service import ChatResponse, Usage
from titan.messaging import service as messaging
from titan.messaging.bus import clear_watchdogs
from titan.messaging.protocol import AgentMessage
from titan.runtime.service import build_runtime, build_watchdog, set_watchdog_config
from titan.runtime.watchdog import (
    WatchdogConfig,
    dedup_key,
    escalation_message_id,
    load_watchdog_config,
)
from titan.settings import Settings

pytestmark = pytest.mark.schema

TURN_JSON = (
    '{"messages": [{"type": "ProgressReport", "to_agent": "pm_project", '
    '"payload": {"status": "in_progress", "percent_complete": 50, "summary": "ok"}}]}'
)


class GatedLlm:
    """Returns a fixed valid turn output; hangs while the gate is cleared (to
    park a turn in-flight so the test can kill the consumer mid-message)."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.gate.set()
        self.waiting = asyncio.Event()
        self.calls = 0

    async def chat(self, _req: Any) -> ChatResponse:
        if not self.gate.is_set():
            self.waiting.set()
            await self.gate.wait()
        self.calls += 1
        return ChatResponse(
            text=TURN_JSON,
            tool_calls=[],
            finish_reason="stop",
            usage=Usage(input_tokens=4, output_tokens=2),
            provider="ollama",
            model_id="scripted",
            cost_usd=Decimal("0"),
            latency_ms=1,
            requested_alias="local-default",
            served_alias="local-default",
        )


def _bus() -> RedisStreamBus:
    return RedisStreamBus(fakeredis.aioredis.FakeRedis(decode_responses=True))


async def _make_project(engine: Any) -> uuid.UUID:
    pid, uid = uuid.uuid4(), uuid.uuid4()
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
    return pid


async def _drain(engine: Any, bus: RedisStreamBus) -> None:
    drainer = OutboxDrainer(engine, bus)
    while await drainer.drain_once():
        pass


def _assignment(pid: uuid.UUID, title: str, *, requires_ack: bool = False) -> AgentMessage:
    return AgentMessage(
        type="TaskAssignment",
        project_id=pid,
        from_agent="pm_project",
        to_agent="eng_backend",
        requires_ack=requires_ack,
        payload={"title": title, "description": "d", "acceptance_criteria": ["x"]},
    )


async def _reply_count(engine: Any, pid: uuid.UUID) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT count(*) FROM messages WHERE project_id = :pid "
                "AND from_agent = 'eng_backend' AND kind = 'progress_report'"
            ),
            {"pid": pid},
        )
        return int(result.scalar_one())


async def _reset_global_config(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM watchdog_config"))


# --- (a) stalled-consumer restart: kill → replace → zero duplicate sends ----


def test_killed_consumer_replaced_with_zero_duplicate_sends(migrated_engine: Any) -> None:
    """The TITAN-AGT-009 acceptance: kill the consumer mid-way through a scripted
    assignment sequence; a replacement resumes (well) within 30 s and NOTHING is
    sent twice — every trigger has exactly one reply (deterministic emission ids)."""

    async def scenario() -> None:
        await _reset_global_config(migrated_engine)
        clear_watchdogs()
        bus = _bus()
        pid = await _make_project(migrated_engine)
        llm = GatedLlm()
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=llm)
        runtime.backoff_base_s = 0.0
        watchdog = build_watchdog(runtime, bus, poll_interval_s=0.02)
        try:
            # Phase 1: two assignments processed normally by the supervised consumer.
            async with migrated_engine.begin() as conn:
                for title in ("t1", "t2"):
                    await messaging.send_agent_message(
                        conn, _assignment(pid, title), actor="pm_project"
                    )
            await _drain(migrated_engine, bus)
            watchdog.supervise(pid, "eng_backend")
            deadline = time.monotonic() + 10
            while await _reply_count(migrated_engine, pid) < 2:
                assert time.monotonic() < deadline, "worker never processed the first batch"
                await asyncio.sleep(0.02)

            # Phase 2: park the NEXT turn in-flight (delivered, unacked), then kill.
            llm.gate.clear()
            async with migrated_engine.begin() as conn:
                for title in ("t3", "t4"):
                    await messaging.send_agent_message(
                        conn, _assignment(pid, title), actor="pm_project"
                    )
            await _drain(migrated_engine, bus)
            await asyncio.wait_for(llm.waiting.wait(), timeout=10)  # t3 is mid-turn
            watchdog.kill(pid, "eng_backend")
            llm.gate.set()

            # Phase 3: the sweep replaces the dead consumer; the replacement seizes
            # the pending entry and resumes the sequence.
            started = time.monotonic()
            report = await watchdog.sweep()
            assert any("eng_backend" in key for key in report.restarted)
            deadline = time.monotonic() + 25
            while await _reply_count(migrated_engine, pid) < 4:
                assert time.monotonic() < deadline, "replacement never finished the sequence"
                await asyncio.sleep(0.02)
            assert time.monotonic() - started < 30  # the DoD bound, with margin

            # Zero duplicate sends: exactly one reply per trigger, nothing extra.
            async with migrated_engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT correlation_id, count(*) FROM messages "
                        "WHERE project_id = :pid AND from_agent = 'eng_backend' "
                        "GROUP BY correlation_id"
                    ),
                    {"pid": pid},
                )
                counts = [int(count) for _corr, count in rows.all()]
            assert counts == [1, 1, 1, 1], f"duplicate sends detected: {counts}"

            # And the replacement is a fresh generation, still alive.
            sup = watchdog._supervised[(pid, "eng_backend")]
            assert sup.generation == 2 and not sup.task.done()
        finally:
            await watchdog.stop()
            clear_watchdogs()

    run_async(scenario)


# --- (b) unacked monitor: type-specific timeout via the existing hook -------


def test_unacked_requires_ack_escalates_after_type_timeout(migrated_engine: Any) -> None:
    """A requires_ack message re-entering the pending set fires the Phase-27 hook;
    the watchdog escalates ONLY once the message is past its TYPE-SPECIFIC timeout
    (§6.3) — and the timeout is live config, no redeploy (TITAN-AGT-020)."""

    async def scenario() -> None:
        await _reset_global_config(migrated_engine)
        clear_watchdogs()
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=GatedLlm())
        watchdog = build_watchdog(runtime, bus)
        watchdog.register()
        try:
            message = _assignment(pid, "stuck work", requires_ack=True)
            async with migrated_engine.begin() as conn:
                await messaging.send_agent_message(conn, message, actor="pm_project")
            await _drain(migrated_engine, bus)
            # Deliver to a consumer that then dies without acking.
            delivered = await messaging.consume_inbox(bus, pid, "eng_backend", "dead-1")
            assert len(delivered) == 1

            # Timeout NOT yet reached (10 s override): the hook must stay silent.
            async with migrated_engine.begin() as conn:
                await set_watchdog_config(
                    conn,
                    project_id=None,
                    stalled_after_ms=960_000,
                    sweep_interval_ms=5_000,
                    reclaim_min_idle_ms=1,
                    ack_timeout_overrides={"TaskAssignment": 10_000},
                )
            await asyncio.sleep(0.05)  # idle past reclaim threshold, not past timeout
            await messaging.reclaim_unacked(bus, pid, "eng_backend", "reaper-1", min_idle_ms=1)
            escalation_id = escalation_message_id(pid, "unacked", "pm_project", dedup_key(message))
            async with migrated_engine.connect() as conn:
                assert await messaging.message_exists(conn, escalation_id) is False

            # Lower the timeout LIVE (no redeploy) → the next reclaim escalates.
            async with migrated_engine.begin() as conn:
                await set_watchdog_config(
                    conn,
                    project_id=None,
                    stalled_after_ms=960_000,
                    sweep_interval_ms=5_000,
                    reclaim_min_idle_ms=1,
                    ack_timeout_overrides={"TaskAssignment": 1},
                )
            await asyncio.sleep(0.05)
            await messaging.reclaim_unacked(bus, pid, "eng_backend", "reaper-2", min_idle_ms=1)
            async with migrated_engine.connect() as conn:
                escalation = await messaging.get_message(conn, escalation_id)
            assert escalation is not None
            # To the recipient's SUPERVISOR through the normal ladder shape.
            assert escalation.from_agent == "orchestrator"
            assert escalation.to_agent == "pm_project"  # eng_backend's supervisor
            assert escalation.kind == "progress_report"
            assert escalation.body["status"] == "blocked"
        finally:
            await watchdog.stop()
            clear_watchdogs()

    run_async(scenario)


# --- (c) escalation dedup on conflict_id / thread_id (§10 R6) ---------------


def test_escalations_dedup_per_dispute(migrated_engine: Any) -> None:
    """Repeated flare-ups of ONE dispute produce exactly one escalation; a
    distinct dispute produces its own. Keyed on conflict_id for conflicts, the
    thread for everything else — never a storm at the CEO/human tier."""

    async def scenario() -> None:
        await _reset_global_config(migrated_engine)
        clear_watchdogs()
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=GatedLlm())
        watchdog = build_watchdog(runtime, bus)
        async with migrated_engine.begin() as conn:
            await set_watchdog_config(
                conn,
                project_id=None,
                stalled_after_ms=960_000,
                sweep_interval_ms=5_000,
                reclaim_min_idle_ms=1,
                ack_timeout_overrides={"ConflictResolution": 1},
            )
        try:
            conflict_id = uuid.uuid4()

            def flare(round_no: int, *, cid: uuid.UUID) -> AgentMessage:
                return AgentMessage(
                    type="ConflictResolution",
                    project_id=pid,
                    from_agent="qa",
                    to_agent="eng_backend",
                    requires_ack=True,
                    thread_id=uuid.uuid4(),  # each flare-up on its own thread
                    payload={
                        "conflict_id": str(cid),
                        "parties": ["qa", "eng_backend"],
                        "subject": "is BUG-1 blocking?",
                        "round": round_no,
                    },
                )

            await asyncio.sleep(0.01)  # age past the 1 ms timeout
            first, second = flare(1, cid=conflict_id), flare(2, cid=conflict_id)
            await watchdog._on_unacked(first)
            await watchdog._on_unacked(second)  # same conflict_id, different thread
            other = flare(1, cid=uuid.uuid4())
            await watchdog._on_unacked(other)  # a DIFFERENT dispute

            async with migrated_engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT count(*) FROM messages WHERE project_id = :pid "
                        "AND from_agent = 'orchestrator' AND kind = 'progress_report'"
                    ),
                    {"pid": pid},
                )
                escalations = int(result.scalar_one())
            assert escalations == 2  # one per dispute, not one per flare-up
        finally:
            await watchdog.stop()
            clear_watchdogs()

    run_async(scenario)


# --- (d) discussion-deadline enforcement (§5.4, TITAN-AGT-014) --------------


def test_expired_discussion_escalates_and_closes(migrated_engine: Any) -> None:
    async def scenario() -> None:
        await _reset_global_config(migrated_engine)
        clear_watchdogs()
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=GatedLlm())
        watchdog = build_watchdog(runtime, bus)
        try:
            thread_id = uuid.uuid4()
            discussion = AgentMessage(
                type="ArchitectureDiscussion",
                project_id=pid,
                from_agent="eng_backend",
                to_agent="architect",
                thread_id=thread_id,
                payload={
                    "question": "sync or async?",
                    "position": "async",
                    "decision_deadline": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                },
            )
            async with migrated_engine.begin() as conn:
                await messaging.send_agent_message(conn, discussion, actor="eng_backend")

            report = await watchdog.sweep()
            assert thread_id in report.discussions_closed

            async with migrated_engine.connect() as conn:
                thread = await messaging.messages_in_thread(conn, pid, thread_id)
                all_msgs = await messaging.messages_for_project(conn, pid)
            # Auto-closed with a DecisionLog recording the expiry, in the thread.
            closes = [m for m in thread if m.kind == "decision_log"]
            assert len(closes) == 1 and closes[0].body["authority"] == "watchdog"
            # Escalated to the moderator's supervisor (architect -> ceo).
            escalations = [
                m for m in all_msgs if m.from_agent == "orchestrator" and m.to_agent == "ceo"
            ]
            assert len(escalations) == 1 and escalations[0].body["status"] == "blocked"

            # Idempotent: a second sweep adds nothing (the thread now holds a
            # DecisionLog, and the escalation id is deterministic).
            second = await watchdog.sweep()
            assert second.discussions_closed == [] and second.escalated == []
            async with migrated_engine.connect() as conn:
                after = await messaging.messages_for_project(conn, pid)
            assert len(after) == len(all_msgs)
        finally:
            await watchdog.stop()
            clear_watchdogs()

    run_async(scenario)


def test_decided_discussion_is_left_alone(migrated_engine: Any) -> None:
    """A thread that already produced a DecisionLog by its deadline is closed
    business — the watchdog never touches it."""

    async def scenario() -> None:
        await _reset_global_config(migrated_engine)
        clear_watchdogs()
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=GatedLlm())
        watchdog = build_watchdog(runtime, bus)
        try:
            thread_id = uuid.uuid4()
            async with migrated_engine.begin() as conn:
                await messaging.send_agent_message(
                    conn,
                    AgentMessage(
                        type="ArchitectureDiscussion",
                        project_id=pid,
                        from_agent="eng_backend",
                        to_agent="architect",
                        thread_id=thread_id,
                        payload={
                            "question": "q",
                            "position": "p",
                            "decision_deadline": (
                                datetime.now(UTC) - timedelta(hours=1)
                            ).isoformat(),
                        },
                    ),
                    actor="eng_backend",
                )
                await messaging.send_agent_message(
                    conn,
                    AgentMessage(
                        type="DecisionLog",
                        project_id=pid,
                        from_agent="architect",
                        to_agent="eng_backend",
                        thread_id=thread_id,
                        payload={
                            "decision": "async",
                            "context": "decided in time",
                            "trade_offs": "none",
                            "authority": "decides_alone",
                            "reversible": True,
                        },
                    ),
                    actor="architect",
                )
            report = await watchdog.sweep()
            assert report.discussions_closed == [] and report.escalated == []
        finally:
            await watchdog.stop()
            clear_watchdogs()

    run_async(scenario)


# --- live config (TITAN-AGT-020) ---------------------------------------------


def test_concurrent_turn_attempt_never_mixes_or_appends(migrated_engine: Any) -> None:
    """Emission is all-or-nothing per attempt (the takeover-race guarantee behind
    zero duplicate sends): if a first attempt for a trigger already committed, a
    SECOND attempt whose (non-deterministic) LLM produced MORE messages must NOT
    append its extras — it returns the winner's set and discards its own output."""
    from titan.runtime.output import AgentTurnOutput, OutboundMessage
    from titan.runtime.turn import _emit_messages

    def progress(summary: str) -> OutboundMessage:
        return OutboundMessage(
            type="ProgressReport",
            to_agent="pm_project",
            payload={"status": "in_progress", "percent_complete": 50, "summary": summary},
        )

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=GatedLlm())
        trigger = _assignment(pid, "contested-turn")

        # Attempt A commits a ONE-message output.
        first = await _emit_messages(
            runtime, pid, "eng_backend", trigger, AgentTurnOutput(messages=(progress("A"),))
        )
        assert len(first) == 1

        # Attempt B (a different, LONGER LLM output for the SAME trigger) must fold
        # onto A's committed set — never append its own second message.
        second = await _emit_messages(
            runtime,
            pid,
            "eng_backend",
            trigger,
            AgentTurnOutput(messages=(progress("B1"), progress("B2"))),
        )
        assert second == first  # the winner's set, not a mix

        async with migrated_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT count(*) FROM messages WHERE project_id = :pid "
                    "AND from_agent = 'eng_backend'"
                ),
                {"pid": pid},
            )
        assert int(result.scalar_one()) == 1  # exactly A's one message, nothing appended

    run_async(scenario)


def test_different_cause_escalations_both_fire_on_one_thread(migrated_engine: Any) -> None:
    """The dedup id is scoped by cause + target, so an unacked-message escalation
    on a thread does NOT suppress a discussion-deadline escalation on the SAME
    thread — a lost escalation is worse than a storm (§10 R6 done right)."""

    async def scenario() -> None:
        await _reset_global_config(migrated_engine)
        clear_watchdogs()
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=GatedLlm())
        watchdog = build_watchdog(runtime, bus)
        thread_id = uuid.uuid4()
        try:
            # An unacked ArchitectureDiscussion-thread review escalates (cause=unacked).
            unacked = AgentMessage(
                type="ReviewRequest",
                project_id=pid,
                from_agent="eng_backend",
                to_agent="architect",
                thread_id=thread_id,
                requires_ack=True,
                payload={"artifact_type": "architecture", "artifact_ref": "a:1", "verdict": None},
            )
            async with migrated_engine.begin() as conn:
                await set_watchdog_config(
                    conn,
                    project_id=None,
                    stalled_after_ms=960_000,
                    sweep_interval_ms=5_000,
                    reclaim_min_idle_ms=1,
                    ack_timeout_overrides={"ReviewRequest": 1},
                )
            await asyncio.sleep(0.01)
            await watchdog._on_unacked(unacked)

            # The SAME thread also expires as a discussion (cause=discussion).
            async with migrated_engine.begin() as conn:
                await messaging.send_agent_message(
                    conn,
                    AgentMessage(
                        type="ArchitectureDiscussion",
                        project_id=pid,
                        from_agent="eng_backend",
                        to_agent="architect",
                        thread_id=thread_id,
                        payload={
                            "question": "q",
                            "position": "p",
                            "decision_deadline": (
                                datetime.now(UTC) - timedelta(hours=1)
                            ).isoformat(),
                        },
                    ),
                    actor="eng_backend",
                )
            report = await watchdog.sweep()
            assert report.escalated, "the discussion escalation was suppressed by the unacked one"

            async with migrated_engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT count(*) FROM messages WHERE project_id = :pid "
                        "AND from_agent = 'orchestrator' AND kind = 'progress_report'"
                    ),
                    {"pid": pid},
                )
            assert int(result.scalar_one()) == 2  # both escalations fired
        finally:
            await watchdog.stop()
            clear_watchdogs()

    run_async(scenario)


def test_malformed_deadline_does_not_poison_the_sweep(migrated_engine: Any) -> None:
    """A model/human-written decision_deadline that is not a valid timestamp must
    degrade that row to 'not expired' — never abort the whole deadline sweep
    (which would kill behavior 4 deployment-wide)."""

    async def scenario() -> None:
        await _reset_global_config(migrated_engine)
        clear_watchdogs()
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=GatedLlm())
        watchdog = build_watchdog(runtime, bus)
        try:
            good_thread, bad_thread = uuid.uuid4(), uuid.uuid4()
            async with migrated_engine.begin() as conn:
                # A garbage deadline (bypasses the strict bus via a raw insert, as a
                # permissive REST post or an epoch-int payload would produce).
                await conn.execute(
                    text(
                        "INSERT INTO messages (id, project_id, thread_id, kind, body, from_agent) "
                        "VALUES (:id, :p, :t, 'architecture_discussion', "
                        "CAST(:b AS jsonb), 'eng_backend')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "p": pid,
                        "t": bad_thread,
                        "b": '{"question": "q", "position": "p", "decision_deadline": "whenever"}',
                    },
                )
                await messaging.send_agent_message(
                    conn,
                    AgentMessage(
                        type="ArchitectureDiscussion",
                        project_id=pid,
                        from_agent="eng_backend",
                        to_agent="architect",
                        thread_id=good_thread,
                        payload={
                            "question": "q",
                            "position": "p",
                            "decision_deadline": (
                                datetime.now(UTC) - timedelta(hours=1)
                            ).isoformat(),
                        },
                    ),
                    actor="eng_backend",
                )
            # The sweep does not raise, and closes ONLY the well-formed expired thread.
            report = await watchdog.sweep()
            assert report.discussions_closed == [good_thread]
        finally:
            await watchdog.stop()
            clear_watchdogs()

    run_async(scenario)


def test_watchdog_config_precedence_and_freshness(migrated_engine: Any) -> None:
    async def scenario() -> None:
        await _reset_global_config(migrated_engine)
        pid = await _make_project(migrated_engine)
        # No rows -> code defaults.
        async with migrated_engine.connect() as conn:
            cfg = await load_watchdog_config(conn, pid)
        assert cfg == WatchdogConfig()
        assert cfg.effective_ack_timeout_ms("TaskAssignment") == 600_000  # bus default

        # Global row applies to every project without one of its own…
        async with migrated_engine.begin() as conn:
            await set_watchdog_config(
                conn,
                project_id=None,
                stalled_after_ms=111,
                sweep_interval_ms=222,
                reclaim_min_idle_ms=333,
                ack_timeout_overrides={"ReviewRequest": 444},
            )
        async with migrated_engine.connect() as conn:
            cfg = await load_watchdog_config(conn, pid)
        assert cfg.stalled_after_ms == 111
        assert cfg.effective_ack_timeout_ms("ReviewRequest") == 444
        assert cfg.effective_ack_timeout_ms("TaskAssignment") == 600_000  # untouched type

        # …and the project row overrides the global one, effective immediately.
        async with migrated_engine.begin() as conn:
            await set_watchdog_config(
                conn,
                project_id=pid,
                stalled_after_ms=999,
                sweep_interval_ms=222,
                reclaim_min_idle_ms=333,
            )
        async with migrated_engine.connect() as conn:
            cfg = await load_watchdog_config(conn, pid)
            global_cfg = await load_watchdog_config(conn)
        assert cfg.stalled_after_ms == 999 and global_cfg.stalled_after_ms == 111

    run_async(scenario)


def test_set_watchdog_config_rejects_bad_values(migrated_engine: Any) -> None:
    """Thresholds and per-type ack overrides are validated at the write boundary,
    so no hot-path config load can hit a zero/negative/non-int value."""

    async def scenario() -> None:
        await _reset_global_config(migrated_engine)
        for bad in ({"TaskAssignment": 0}, {"ReviewRequest": -5}):
            with pytest.raises(ValueError):
                async with migrated_engine.begin() as conn:
                    await set_watchdog_config(
                        conn,
                        project_id=None,
                        stalled_after_ms=1000,
                        sweep_interval_ms=1000,
                        reclaim_min_idle_ms=1000,
                        ack_timeout_overrides=bad,
                    )
        with pytest.raises(ValueError):
            async with migrated_engine.begin() as conn:
                await set_watchdog_config(
                    conn,
                    project_id=None,
                    stalled_after_ms=0,
                    sweep_interval_ms=1000,
                    reclaim_min_idle_ms=1000,
                )

    run_async(scenario)
