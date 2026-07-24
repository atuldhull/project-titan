"""The agent runtime turn loop, end to end (Vol 2 §8, TITAN-AGT-002/004/023/024).

Drives real turns through Redis Streams + PostgreSQL with a SCRIPTED provider:
a two-agent exchange (TaskAssignment → ProgressReport → ReviewRequest →
DecisionLog) every message of which is persisted and replayable; a kill/restart
mid-exchange that loses no work and duplicates no side effect; a prompt-injected
ungranted tool call rejected + logged as a security event while a granted tool
executes and is audited; and the self-critique retry on invalid output.

Needs the disposable test DB (`migrated_engine`) + fakeredis."""

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.events.outbox import OutboxDrainer
from titan.events.redis_bus import RedisStreamBus
from titan.llm.service import ChatResponse, ToolCall, Usage
from titan.messaging import service as messaging
from titan.messaging.service import AgentMessage
from titan.runtime.service import build_runtime, build_worker
from titan.runtime.turn import run_turn
from titan.settings import Settings
from titan.tools.service import ExecutionOutcome, ToolExecutionContext, ToolSpec

pytestmark = pytest.mark.schema


# --- a scripted provider (the DoD's "scripted/mock provider") --------------


class ScriptedLlm:
    """Returns a queued ChatResponse per agent (by ``metadata.agent_id``), FIFO,
    and records every request so tests can assert what tools were exposed. A
    queued ``BaseException`` is RAISED instead of returned — used to inject a
    transient gateway failure for the rung-1 retry."""

    def __init__(self, script: "Mapping[str, Sequence[ChatResponse | BaseException]]") -> None:
        self._script: dict[str, list[ChatResponse | BaseException]] = {
            k: list(v) for k, v in script.items()
        }
        self.requests: list[Any] = []

    async def chat(self, req: Any) -> ChatResponse:
        self.requests.append(req)
        agent = req.metadata.agent_id or ""
        queue = self._script.get(agent)
        if not queue:
            raise AssertionError(f"no scripted response for {agent}")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def remaining(self, agent: str) -> list[ChatResponse | BaseException]:
        """Responses not yet consumed for ``agent`` — lets a test prove a code
        path did (or did NOT) call the model."""
        return self._script.get(agent, [])


def _resp(*, text: str | None = None, tool_calls: list[ToolCall] | None = None) -> ChatResponse:
    calls = tool_calls or []
    return ChatResponse(
        text=text,
        tool_calls=calls,
        finish_reason="tool_use" if calls else "stop",
        usage=Usage(input_tokens=12, output_tokens=6),
        provider="ollama",
        model_id="qwen3-coder:30b",
        cost_usd=Decimal("0"),
        latency_ms=1,
        requested_alias="local-default",
        served_alias="local-default",
    )


def _turn_json(messages: list[dict[str, Any]]) -> str:
    import json

    return json.dumps({"messages": messages})


# --- fixtures --------------------------------------------------------------


def _bus() -> RedisStreamBus:
    return RedisStreamBus(fakeredis.aioredis.FakeRedis(decode_responses=True))


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


async def _drain(engine: Any, bus: RedisStreamBus) -> None:
    drainer = OutboxDrainer(engine, bus)
    while await drainer.drain_once():
        pass


def _seed(pid: uuid.UUID, to: str = "eng_backend") -> AgentMessage:
    return AgentMessage(
        type="TaskAssignment",
        project_id=pid,
        from_agent="pm_project",
        to_agent=to,
        payload={
            "title": "Build the widget",
            "description": "make it",
            "acceptance_criteria": ["x"],
        },
    )


# --- the two-agent exchange (the DoD headline) -----------------------------


def test_two_agent_exchange_persists_and_is_replayable(migrated_engine: Any) -> None:
    """TaskAssignment → ProgressReport → ReviewRequest → DecisionLog end to end
    through Redis Streams; every message persisted, one shared conversation."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        script = {
            "agent.eng_backend": [
                _resp(
                    text=_turn_json(
                        [
                            {
                                "type": "ProgressReport",
                                "to_agent": "pm_project",
                                "payload": {
                                    "status": "in_progress",
                                    "percent_complete": 60,
                                    "summary": "widget scaffolded",
                                },
                            },
                            {
                                "type": "ReviewRequest",
                                "to_agent": "reviewer",
                                "payload": {
                                    "artifact_type": "pull_request",
                                    "artifact_ref": "pr/1",
                                    "checklist": ["tests", "lint"],
                                },
                            },
                        ]
                    )
                )
            ],
            "agent.reviewer": [
                _resp(
                    text=_turn_json(
                        [
                            {
                                "type": "DecisionLog",
                                "to_agent": "pm_project",
                                "payload": {
                                    "decision": "approve PR #1",
                                    "context": "widget looks correct",
                                    "alternatives": [],
                                    "trade_offs": "none material",
                                    "authority": "reviewer",
                                    "reversible": True,
                                },
                            }
                        ]
                    )
                )
            ],
        }
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )

        seed = _seed(pid)
        async with migrated_engine.begin() as conn:
            await messaging.send_agent_message(conn, seed, actor="pm_project")
        await _drain(migrated_engine, bus)

        # Backend turn → emits ProgressReport + ReviewRequest.
        backend = build_worker(runtime, bus, pid, "eng_backend")
        results = await backend.process_available()
        assert len(results) == 1 and results[0].completed
        assert len(results[0].emitted_message_ids) == 2
        await _drain(migrated_engine, bus)

        # Reviewer turn → emits DecisionLog.
        reviewer = build_worker(runtime, bus, pid, "reviewer")
        rresults = await reviewer.process_available()
        assert len(rresults) == 1 and rresults[0].completed
        await _drain(migrated_engine, bus)

        # Every message is persisted (replayable) and shares one thread + correlation.
        async with migrated_engine.connect() as conn:
            persisted = await messaging.messages_for_project(conn, pid)
        kinds = sorted(m.kind for m in persisted)
        assert kinds == ["decision_log", "progress_report", "review_request", "task_assignment"]
        assert {m.correlation_id for m in persisted} == {seed.correlation_id}
        assert {m.thread_id for m in persisted} == {seed.correlation_id}

    run_async(scenario)


# --- kill/restart mid-exchange: no loss, no duplicate side effects ---------


def _report(summary: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    msg = {
        "type": "ProgressReport",
        "to_agent": "pm_project",
        "payload": {"status": "completed", "percent_complete": 100, "summary": summary},
    }
    return {**msg, **(extra or {})}


def test_kill_restart_reprocesses_without_duplicate_emission(migrated_engine: Any) -> None:
    """A worker crashes AFTER emitting but BEFORE acking. A restarted worker
    reclaims the message and reprocesses it. Even though the reprocess pass is
    scripted to return a DIFFERENT and LONGER message list (as a real
    non-deterministic provider would), the completed-turn short-circuit re-runs
    neither the LLM nor emission — no message is duplicated (TITAN-AGT-002/016)."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        script = {
            "agent.eng_backend": [
                _resp(text=_turn_json([_report("done")])),
                # The reprocess pass would emit TWO different messages if it ran —
                # a divergent, longer list. The short-circuit must ignore it wholesale.
                _resp(text=_turn_json([_report("done again"), _report("and again")])),
            ]
        }
        llm = ScriptedLlm(script)
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=llm)
        seed = _seed(pid)
        async with migrated_engine.begin() as conn:
            await messaging.send_agent_message(conn, seed, actor="pm_project")
        await _drain(migrated_engine, bus)

        # "Crash": consume + run the turn (emits, committed) but never ack.
        got = await messaging.consume_inbox(bus, pid, "eng_backend", "dead", block_ms=100)
        assert len(got) == 1
        first = await run_turn(
            runtime, pid, "eng_backend", messaging.parse_agent_message(got[0].envelope.payload)
        )
        assert first.completed and len(first.emitted_message_ids) == 1
        await _drain(migrated_engine, bus)

        count_after_crash = await _message_count(migrated_engine, pid)
        assert count_after_crash == 2  # seed + one ProgressReport

        # "Restart": a fresh worker reclaims the unacked message and reprocesses.
        restarted = build_worker(runtime, bus, pid, "eng_backend", consumer="restarted")
        reprocessed = await restarted.process_reclaimed(min_idle_ms=0)
        assert len(reprocessed) == 1 and reprocessed[0].completed
        # Short-circuited: same single id, the divergent 2nd response never consumed.
        assert reprocessed[0].emitted_message_ids == first.emitted_message_ids
        assert len(llm.remaining("agent.eng_backend")) == 1  # 2nd response untouched
        await _drain(migrated_engine, bus)
        assert await _message_count(migrated_engine, pid) == 2  # NO duplicate

    run_async(scenario)


# --- tool-grant enforcement + audit (TITAN-AGT-004/024) --------------------


async def _echo(arguments: dict[str, Any], context: ToolExecutionContext) -> ExecutionOutcome:
    return ExecutionOutcome(result={"echo": arguments})


def _grant_tool(runtime: Any, agent_id: str, tool: str) -> None:
    """Replace the runtime's registry with a fresh one that grants ``tool`` to
    ``agent_id`` — never mutate the lru_cached roster shared across tests."""
    from titan.agents.service import AgentRegistry

    defs = {d.agent_id: d for d in runtime.registry.all()}
    base = defs[agent_id]
    defs[agent_id] = base.model_copy(update={"tool_grants": (*base.tool_grants, tool)})
    runtime.registry = AgentRegistry(defs)


def _register_echo(runtime: Any) -> None:
    runtime.tool_registry.register(
        ToolSpec(
            name="code.echo",
            provider="builtin:test",
            description="echo the arguments back",
            risk="read",
            required_scopes=(),
            input_schema={"type": "object", "additionalProperties": True},
            output_schema={"type": "object", "additionalProperties": True},
            executor=_echo,
            timeout_s=5.0,
        )
    )


def test_ungranted_tool_rejected_granted_tool_audited(migrated_engine: Any) -> None:
    """A prompt-injected ungranted tool call is refused at the runtime layer and
    logged as a security event; a granted tool executes through the Tool Bus and
    leaves an audit row (TITAN-AGT-004 + TITAN-AGT-024)."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        # Backend asks for an UNGRANTED tool first (injection), then a GRANTED
        # tool, then returns its final structured output.
        script = {
            "agent.eng_backend": [
                _resp(
                    tool_calls=[ToolCall(id="c1", name="secret.read", arguments={"path": "/etc/x"})]
                ),
                _resp(tool_calls=[ToolCall(id="c2", name="code.echo", arguments={"msg": "hi"})]),
                _resp(
                    text=_turn_json(
                        [
                            {
                                "type": "ProgressReport",
                                "to_agent": "pm_project",
                                "payload": {
                                    "status": "in_progress",
                                    "percent_complete": 10,
                                    "summary": "probing",
                                },
                            }
                        ]
                    )
                ),
            ]
        }
        llm = ScriptedLlm(script)
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=llm)
        _register_echo(runtime)
        # Grant code.echo to eng_backend for this turn (its real grants don't map
        # to a registered tool yet); secret.read stays ungranted. Build a FRESH
        # registry (never mutate the lru_cached roster shared across tests).
        _grant_tool(runtime, "eng_backend", "code.echo")

        seed = _seed(pid)
        async with migrated_engine.begin() as conn:
            await messaging.send_agent_message(conn, seed, actor="pm_project")
        await _drain(migrated_engine, bus)

        result = await run_turn(runtime, pid, "eng_backend", seed)
        assert result.completed

        # The ungranted call was refused BEFORE the bus → one security event.
        assert [e.tool for e in result.security_events] == ["secret.read"]
        # The granted call executed.
        granted = [t for t in result.tool_calls if t.disposition == "granted"]
        assert [t.tool for t in granted] == ["code.echo"] and granted[0].status == "ok"

        # Audit: code.echo has audit rows (started + ok); secret.read has NONE
        # (it never reached the Tool Bus).
        async with migrated_engine.connect() as conn:
            audited = (
                (
                    await conn.execute(
                        text("SELECT DISTINCT tool FROM tool_audit WHERE project_id = :p"),
                        {"p": pid},
                    )
                )
                .scalars()
                .all()
            )
        assert "code.echo" in audited
        assert "secret.read" not in audited

        # The scripted request that offered tools never exposed the ungranted one.
        tool_reqs = [r for r in llm.requests if r.tools]
        for req in tool_reqs:
            names = {t.name for t in req.tools}
            assert "secret.read" not in names

    run_async(scenario)


# --- self-critique retry on invalid output (TITAN-AGT-023) -----------------


def test_invalid_output_triggers_self_critique_then_succeeds(migrated_engine: Any) -> None:
    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        script = {
            "agent.eng_backend": [
                _resp(text="I will build it. (not json)"),  # invalid → retry
                _resp(
                    text=_turn_json(
                        [
                            {
                                "type": "ProgressReport",
                                "to_agent": "pm_project",
                                "payload": {
                                    "status": "in_progress",
                                    "percent_complete": 25,
                                    "summary": "corrected",
                                },
                            }
                        ]
                    )
                ),
            ]
        }
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        seed = _seed(pid)
        result = await run_turn(runtime, pid, "eng_backend", seed)
        assert result.completed
        assert result.output_retries == 1  # one self-critique round
        assert len(result.emitted_message_ids) == 1

    run_async(scenario)


def test_invalid_output_never_silently_accepted_then_escalates(migrated_engine: Any) -> None:
    """After the self-critique budget is exhausted the turn is never silently
    accepted — the recovery ladder escalates (rung 3) a blocked ProgressReport to
    the agent's supervisor (TITAN-AGT-023 + §2.2)."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        script = {"agent.eng_backend": [_resp(text="nope") for _ in range(5)]}
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        runtime.backoff_base_s = 0.0
        result = await run_turn(runtime, pid, "eng_backend", _seed(pid))
        assert result.outcome == "escalated"  # NOT silently accepted
        assert result.failure_reason == "invalid_output"
        assert result.output_retries == runtime.max_self_critique == 2  # 2 retries issued
        assert result.escalation_target == "pm_project"  # eng_backend's supervisor
        assert "rung3" in result.recovery_rungs and "rung2" in result.recovery_rungs
        # The escalation is a persisted blocked ProgressReport to the supervisor.
        assert len(result.emitted_message_ids) == 1
        async with migrated_engine.connect() as conn:
            rows = await messaging.messages_for_project(conn, pid)
        blocked = [m for m in rows if m.kind == "progress_report" and m.to_agent == "pm_project"]
        assert len(blocked) == 1 and blocked[0].body["status"] == "blocked"

    run_async(scenario)


def test_malformed_task_id_drives_self_critique_not_infra_error(migrated_engine: Any) -> None:
    """A model output whose task_id is not a UUID is REJECTED at validation and
    drives the self-critique retry (TITAN-AGT-023) — it must NOT crash the emit
    transaction into the infra-error/DLQ path (which would lose the reply and
    re-run tools)."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        bad = {
            "type": "ProgressReport",
            "to_agent": "pm_project",
            "task_id": "not-a-uuid",  # a plausible/adversarial malformed field
            "payload": {"status": "in_progress", "percent_complete": 10, "summary": "x"},
        }
        script = {
            "agent.eng_backend": [
                _resp(text=_turn_json([bad])),  # invalid task_id → self-critique
                _resp(text=_turn_json([_report("corrected")])),
            ]
        }
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        result = await run_turn(runtime, pid, "eng_backend", _seed(pid))
        assert result.completed  # recovered via self-critique, not an infra error
        assert result.outcome != "error"
        assert result.output_retries == 1
        assert len(result.emitted_message_ids) == 1

    run_async(scenario)


def test_tool_spam_past_budget_is_tool_budget_exhausted(migrated_engine: Any) -> None:
    """A model that keeps requesting a granted tool past its budget terminates as
    tool_budget_exhausted (a distinct outcome), not misclassified as invalid
    output — and the loop provably ends."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        # More tool-call responses than the tool-iteration budget.
        script = {
            "agent.eng_backend": [
                _resp(tool_calls=[ToolCall(id=f"c{i}", name="code.echo", arguments={"n": i})])
                for i in range(8)
            ]
        }
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        _register_echo(runtime)
        _grant_tool(runtime, "eng_backend", "code.echo")
        result = await run_turn(runtime, pid, "eng_backend", _seed(pid))
        # A tool-budget failure is a distinct reason that then escalates (rung 3).
        assert result.outcome == "escalated"
        assert result.failure_reason == "tool_budget_exhausted"
        # Exactly max_tool_iterations tools ran, then the budget tripped.
        granted = [t for t in result.tool_calls if t.disposition == "granted"]
        assert len(granted) == runtime.max_tool_iterations == 4

    run_async(scenario)


def test_repeated_identical_tool_call_is_halted_as_loop(migrated_engine: Any) -> None:
    """TITAN-SEC-083: a model repeating the SAME tool call is halted BEFORE the
    Nth (default 3) execution — outcome escalated (paused for review via the
    rung-3 supervisor report), reason loop_detected — so the runaway ends within
    N+1 iterations having run only N-1 tools, and the KPI counter increments."""
    from titan.runtime import metrics

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        # The identical call, verbatim, more times than any budget — a runaway.
        script = {
            "agent.eng_backend": [
                _resp(tool_calls=[ToolCall(id=f"c{i}", name="code.echo", arguments={"n": 1})])
                for i in range(8)
            ]
        }
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        # Keep the turn focused on loop detection; skip working-memory hydration.
        runtime.coordinator = None
        _register_echo(runtime)
        _grant_tool(runtime, "eng_backend", "code.echo")
        assert runtime.loop_guard.enabled  # default Settings enable it, N=3

        before = metrics.AGENT_LOOPS_DETECTED_TOTAL.labels(
            "agent.eng_backend", "repeat"
        )._value.get()
        task_id = uuid.uuid4()  # the loop guard keys per task; the escalation FKs it
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO tasks (id, project_id, title) VALUES (:id, :p, 'looping task')"),
                {"id": task_id, "p": pid},
            )
        trigger = AgentMessage(
            type="TaskAssignment",
            project_id=pid,
            task_id=task_id,
            from_agent="pm_project",
            to_agent="eng_backend",
            payload={"title": "loop", "description": "d", "acceptance_criteria": ["x"]},
        )
        result = await run_turn(runtime, pid, "eng_backend", trigger)

        assert result.outcome == "escalated"  # paused for review
        assert result.failure_reason == "loop_detected"
        # The 3rd identical call was blocked BEFORE the bus: only N-1 = 2 executed.
        granted = [t for t in result.tool_calls if t.disposition == "granted"]
        assert len(granted) == 2
        after = metrics.AGENT_LOOPS_DETECTED_TOTAL.labels(
            "agent.eng_backend", "repeat"
        )._value.get()
        assert after == before + 1  # the audit/KPI signal fired

    run_async(scenario)


# --- helpers ---------------------------------------------------------------


async def _message_count(engine: Any, pid: uuid.UUID) -> int:
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM messages WHERE project_id = :p"), {"p": pid}
            )
        ).scalar_one()
    return int(count)
