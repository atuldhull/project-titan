"""The failure-recovery ladder + KPI emission (Vol 2 §2.2/§4.x, TITAN-AGT-005/
010/023). Fault-injection across all four rungs, the YAML-driven per-agent
overrides (CEO→human, DB skip-retries, Security/Reviewer fail-closed, Docs/Critic
fail-open), and the Prometheus KPI series. Needs the test DB + fakeredis."""

from typing import Any

import pytest

from tests.conftest import run_async
from tests.test_runtime_turn import (
    ScriptedLlm,
    _bus,
    _drain,
    _make_project,
    _message_count,
    _report,
    _resp,
    _seed,
    _turn_json,
)
from titan.messaging import service as messaging
from titan.messaging.service import AgentMessage
from titan.runtime import metrics
from titan.runtime.service import build_runtime, build_worker
from titan.runtime.turn import run_turn
from titan.settings import Settings

pytestmark = pytest.mark.schema


def _install_span_exporter() -> tuple[Any, Any]:
    """Point the runtime tracer at an in-memory exporter; returns (exporter,
    restore)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from titan.runtime import turn as turn_mod

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original = turn_mod._TRACER
    turn_mod._TRACER = provider.get_tracer("test")

    def restore() -> None:
        turn_mod._TRACER = original

    return exporter, restore


def _reassignment() -> str:
    return _turn_json(
        [
            {
                "type": "TaskAssignment",
                "to_agent": "eng_backend",
                "payload": {
                    "title": "retry the widget",
                    "description": "reassigned after block",
                    "acceptance_criteria": ["x"],
                },
            }
        ]
    )


def test_all_four_rungs_fire_with_one_span_each(migrated_engine: Any) -> None:
    """Drive rung 1 (transient retry) → rung 2 (self-critique) → rung 3 (escalate
    to supervisor) → rung 4 (supervisor handles the blocked report). One OTel span
    per rung across the flow (fault-injection acceptance of TITAN-AGT-005)."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        script: dict[str, list[Any]] = {
            # rung 1: a transient failure, then three invalid outputs → rung 2 ×2 → rung 3.
            "agent.eng_backend": [
                TimeoutError("provider blip"),
                _resp(text="nope"),
                _resp(text="nope"),
                _resp(text="nope"),
            ],
            # rung 4: the supervisor reassigns on receiving the blocked report.
            "agent.pm_project": [_resp(text=_reassignment())],
        }
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        runtime.backoff_base_s = 0.0
        exporter, restore = _install_span_exporter()
        try:
            async with migrated_engine.begin() as conn:
                await messaging.send_agent_message(conn, _seed(pid), actor="pm_project")
            await _drain(migrated_engine, bus)

            eng = await build_worker(runtime, bus, pid, "eng_backend").process_available()
            assert eng[0].outcome == "escalated" and eng[0].escalation_target == "pm_project"
            assert eng[0].transient_retries == 1 and eng[0].output_retries == 2
            await _drain(migrated_engine, bus)

            pm = await build_worker(runtime, bus, pid, "pm_project").process_available()
            assert pm[0].completed and "rung4" in pm[0].recovery_rungs
        finally:
            restore()

        names = {s.name for s in exporter.get_finished_spans()}
        assert {"recovery.rung1", "recovery.rung2", "recovery.rung3", "recovery.rung4"} <= names

    run_async(scenario)


def test_rung1_transient_retry_then_succeeds(migrated_engine: Any) -> None:
    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        script: dict[str, list[Any]] = {
            "agent.eng_backend": [TimeoutError("blip"), _resp(text=_turn_json([_report("ok")]))]
        }
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        runtime.backoff_base_s = 0.0
        result = await run_turn(runtime, pid, "eng_backend", _seed(pid))
        assert result.completed
        assert result.transient_retries == 1 and result.recovery_rungs == ["rung1"]
        assert len(result.emitted_message_ids) == 1

    run_async(scenario)


def test_non_transient_error_is_infra_error_not_escalation(migrated_engine: Any) -> None:
    """A non-transient failure (a bug, a config/budget error) is NOT retried and
    NOT escalated to a supervisor who cannot fix it — it stays an infra 'error'
    (left pending for reclaim)."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        script = {"agent.eng_backend": [ValueError("boom")]}
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        result = await run_turn(runtime, pid, "eng_backend", _seed(pid))
        assert result.outcome == "error" and result.should_ack is False
        assert result.recovery_rungs == [] and result.emitted_message_ids == []

    run_async(scenario)


def _fails_thrice(agent: str) -> dict[str, list[Any]]:
    return {f"agent.{agent}": [_resp(text="nope") for _ in range(3)]}


def test_fail_open_agent_does_not_escalate(migrated_engine: Any) -> None:
    """Docs (§4.13, fail-open): an exhausted turn is logged, never escalated —
    the pipeline is not blocked and no supervisor message is emitted."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(_fails_thrice("docs"))
        )
        runtime.backoff_base_s = 0.0
        trigger = AgentMessage(
            type="TaskAssignment",
            project_id=pid,
            from_agent="pm_project",
            to_agent="docs",
            payload={"title": "write README", "description": "d", "acceptance_criteria": ["x"]},
        )
        result = await run_turn(runtime, pid, "docs", trigger)
        assert result.outcome == "failed_open"
        assert result.emitted_message_ids == []  # nothing blocked, pipeline proceeds
        assert "rung3" not in result.recovery_rungs and result.escalation_target is None

    run_async(scenario)


def test_fail_closed_agent_escalates(migrated_engine: Any) -> None:
    """Security (§4.11, fail-closed, TITAN-AGT-010): an exhausted turn escalates a
    blocked report so the gated action queues rather than proceeds."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(
            Settings(),
            migrated_engine,
            redis=bus._redis,
            llm=ScriptedLlm(_fails_thrice("security")),
        )
        runtime.backoff_base_s = 0.0
        trigger = AgentMessage(
            type="ReviewRequest",
            project_id=pid,
            from_agent="eng_backend",
            to_agent="security",
            payload={"artifact_type": "pull_request", "artifact_ref": "pr/9"},
        )
        result = await run_turn(runtime, pid, "security", trigger)
        assert result.outcome == "escalated"  # NOT auto-approved on failure
        assert result.escalation_target == "pm_project" and len(result.emitted_message_ids) == 1

    run_async(scenario)


def test_ceo_escalates_to_human(migrated_engine: Any) -> None:
    """CEO (§4.1): no supervisor agent — rung 3 goes to the human client."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(_fails_thrice("ceo"))
        )
        runtime.backoff_base_s = 0.0
        trigger = AgentMessage(
            type="TaskAssignment",
            project_id=pid,
            from_agent="human",
            to_agent="ceo",
            payload={"title": "set the vision", "description": "d", "acceptance_criteria": ["x"]},
        )
        result = await run_turn(runtime, pid, "ceo", trigger)
        assert result.outcome == "escalated" and result.escalation_target == "human"
        async with migrated_engine.connect() as conn:
            rows = await messaging.messages_for_project(conn, pid)
        memo = [m for m in rows if m.to_agent == "human" and m.kind == "progress_report"]
        assert len(memo) == 1 and memo[0].body["status"] == "blocked"

    run_async(scenario)


def test_db_destructive_migration_skips_retries(migrated_engine: Any) -> None:
    """Database Engineer (§4.8): a destructive migration that fails escalates
    IMMEDIATELY — rungs 1–2 are skipped (skip_retries_on), so a single invalid
    output escalates with zero self-critique retries."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        # Only ONE invalid response is scripted: if retries were NOT skipped the
        # turn would demand more and the scripted client would raise.
        llm = ScriptedLlm({"agent.eng_database": [_resp(text="nope")]})
        runtime = build_runtime(Settings(), migrated_engine, redis=bus._redis, llm=llm)
        runtime.backoff_base_s = 0.0
        trigger = AgentMessage(
            type="TaskAssignment",
            project_id=pid,
            from_agent="pm_project",
            to_agent="eng_database",
            payload={
                "title": "Destructive migration: drop legacy users table",
                "description": "d",
                "acceptance_criteria": ["x"],
            },
        )
        result = await run_turn(runtime, pid, "eng_database", trigger)
        assert result.outcome == "escalated"
        assert result.output_retries == 0  # rungs 1–2 skipped
        assert "rung2" not in result.recovery_rungs
        assert llm.remaining("agent.eng_database") == []  # exactly one LLM call

    run_async(scenario)


def test_replayed_escalated_turn_reports_escalated_not_completed(migrated_engine: Any) -> None:
    """A turn that escalated (rung 3) then crashed before XACK must, on reclaim,
    be reported as 'escalated' — NOT mislabelled 'completed' (which would count a
    failure as a success in the §2.2 KPIs) — and must not duplicate the block."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        script = {"agent.eng_backend": [_resp(text="nope") for _ in range(3)]}
        runtime = build_runtime(
            Settings(), migrated_engine, redis=bus._redis, llm=ScriptedLlm(script)
        )
        runtime.backoff_base_s = 0.0

        async with migrated_engine.begin() as conn:
            await messaging.send_agent_message(conn, _seed(pid), actor="pm_project")
        await _drain(migrated_engine, bus)

        # "Crash": consume + run the turn (escalates, emits blocked report) but never ack.
        got = await messaging.consume_inbox(bus, pid, "eng_backend", "dead", block_ms=100)
        first = await run_turn(
            runtime, pid, "eng_backend", messaging.parse_agent_message(got[0].envelope.payload)
        )
        assert first.outcome == "escalated"
        await _drain(migrated_engine, bus)
        assert await _message_count(migrated_engine, pid) == 2  # seed + blocked report

        # "Restart": reclaim + reprocess → recovers the true outcome, no duplicate.
        reproc = await build_worker(
            runtime, bus, pid, "eng_backend", consumer="restarted"
        ).process_reclaimed(min_idle_ms=0)
        assert len(reproc) == 1 and reproc[0].outcome == "escalated"  # not "completed"
        assert reproc[0].escalation_target == "pm_project"
        await _drain(migrated_engine, bus)
        assert await _message_count(migrated_engine, pid) == 2  # NO duplicate escalation

    run_async(scenario)


def test_first_pass_kpi_counts_zero_retry_completions(migrated_engine: Any) -> None:
    """The first-pass-acceptance KPI (§2.2) is a dedicated series: a completed turn
    with zero self-critique retries increments first_pass=true."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(
            Settings(),
            migrated_engine,
            redis=bus._redis,
            llm=ScriptedLlm({"agent.eng_backend": [_resp(text=_turn_json([_report("ok")]))]}),
        )
        before = metrics.AGENT_FIRST_PASS_TOTAL.labels("agent.eng_backend", "true")._value.get()
        result = await run_turn(runtime, pid, "eng_backend", _seed(pid))
        assert result.completed and result.output_retries == 0
        after = metrics.AGENT_FIRST_PASS_TOTAL.labels("agent.eng_backend", "true")._value.get()
        assert after == before + 1

    run_async(scenario)


def test_kpi_metrics_emitted_during_recovery(migrated_engine: Any) -> None:
    """KPI series populate in Prometheus during a run: declared KPIs from YAML,
    recovery-rung counters, and the escalation counter (§2.2 KPI emission)."""

    async def scenario() -> None:
        bus = _bus()
        pid = await _make_project(migrated_engine)
        runtime = build_runtime(
            Settings(),
            migrated_engine,
            redis=bus._redis,
            llm=ScriptedLlm(_fails_thrice("eng_backend")),
        )
        runtime.backoff_base_s = 0.0

        # build_runtime declared every agent's YAML kpis to Prometheus.
        declared = {
            s.labels["agent"] for m in metrics.AGENT_KPI_DECLARED.collect() for s in m.samples
        }
        assert "agent.eng_backend" in declared

        before = metrics.AGENT_ESCALATIONS_TOTAL.labels(
            "agent.eng_backend", "pm_project"
        )._value.get()
        rung3_before = metrics.AGENT_RECOVERY_RUNGS_TOTAL.labels(
            "agent.eng_backend", "rung3"
        )._value.get()

        result = await run_turn(runtime, pid, "eng_backend", _seed(pid))
        assert result.outcome == "escalated"

        after = metrics.AGENT_ESCALATIONS_TOTAL.labels(
            "agent.eng_backend", "pm_project"
        )._value.get()
        rung3_after = metrics.AGENT_RECOVERY_RUNGS_TOTAL.labels(
            "agent.eng_backend", "rung3"
        )._value.get()
        assert after == before + 1
        assert rung3_after == rung3_before + 1

    run_async(scenario)
