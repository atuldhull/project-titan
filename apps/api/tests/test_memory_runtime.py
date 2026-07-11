"""Phase 38 — working memory, context budgets & runtime memory wiring
(Vol 5 §4.5, §8.1–8.2, §13; TITAN-BRN-002/-009/-015/-016/-017/-024).

Covers the DoD: the adversarial-tool-output fuzz produces ZERO context
overflows across 1,000+ gateway calls; task completion with unflushed
working-memory events is rejected; a full turn writes back through the real
event path (searchable memory, ledger, KG hook); flushing Redis does not
change a retrieval-only replay; the rolling summary keeps its five-field
schema and survives via the PostgreSQL checkpoint; agents cannot write
org-scope records on either surface.

Uses the Phase-29 rig (scripted provider + fakeredis + migrated_engine) and
the Phase-36/37 memory fakes."""

from __future__ import annotations

import json
import random
import uuid
from typing import Any, cast

import fakeredis.aioredis
import pytest
from pydantic import ValidationError
from sqlalchemy import text

from tests.conftest import run_async
from tests.memory_fakes import HashingEmbeddings
from tests.memory_harness import Pipeline, overlap_scorer, quiesce_outbox
from tests.test_runtime_turn import (
    ScriptedLlm,
    _grant_tool,
    _make_project,
    _resp,
    _turn_json,
)
from tests.test_transitions_api import _agent, _drive_to_in_review, _project, _task, _transition
from titan.llm.service import ChatResponse
from titan.memory import service as memory_service
from titan.memory.retrieval import CrossEncoderReranker
from titan.memory.working import RollingSummary, SummaryFolder, WorkingMemory, events_key
from titan.messaging.service import AgentMessage
from titan.runtime.budget import (
    DEFAULT_CONTEXT_WINDOW,
    TRUNCATION_MARKER,
    ContextBudget,
    fit_text,
    fit_turns,
)
from titan.runtime.memory import (
    MemoryRequest,
    NoopKgSink,
    PacketMemoryProvider,
    WorkingMemoryCoordinator,
)
from titan.runtime.service import build_runtime
from titan.runtime.turn import run_turn
from titan.settings import Settings

pytestmark = pytest.mark.schema

_COUNT = memory_service.count_tokens


# --- §8.1: the budget allocator (pure) ---------------------------------------------


def test_budget_slots_match_the_table() -> None:
    b = ContextBudget.for_window(32_768)
    assert b.system == min(int(32_768 * 0.08), 3_000) == 2_621
    assert b.identity == 1_966
    assert b.task_instruction == 2_621
    assert b.memory_packet == 9_830
    assert b.working_summary == 3_276
    assert b.recent_turns == 9_175
    assert b.response_reserve == 3_276
    # Shares sum to 100 %: the prompt side can never cross the ceiling.
    prompt_side = (
        b.system
        + b.identity
        + b.task_instruction
        + b.memory_packet
        + b.working_summary
        + b.recent_turns
    )
    assert prompt_side <= b.prompt_ceiling
    # Large windows hit the absolute caps.
    big = ContextBudget.for_window(1_000_000)
    assert big.memory_packet == 12_000
    assert big.response_reserve == 8_000
    # No window in the registry -> the documented default.
    assert ContextBudget.for_window(None).window == DEFAULT_CONTEXT_WINDOW


def test_fit_text_head_preserving_with_marker() -> None:
    text_in = " ".join(f"w{i}" for i in range(500))
    out = fit_text(text_in, 50)
    assert out.endswith(TRUNCATION_MARKER)
    assert _COUNT(out) <= 50
    assert out.startswith("w0 w1")  # the head survives
    assert fit_text("short", 50) == "short"  # under budget: untouched


def test_fit_turns_keeps_newest_contiguous_suffix() -> None:
    turns = [f"turn {i} " + "x " * 30 for i in range(10)]
    kept, evicted = fit_turns(turns, 120)
    assert kept == turns[len(evicted) :]  # a strict suffix — never interleaved
    assert evicted == turns[: len(evicted)]
    assert evicted, "the budget must have evicted the oldest turns"
    # A single oversized newest turn is truncated, never dropped.
    kept2, evicted2 = fit_turns(["giant " * 500], 40)
    assert len(kept2) == 1 and evicted2 == []
    assert kept2[0].endswith(TRUNCATION_MARKER)


# --- TITAN-BRN-015 DoD: 1,000-call adversarial overflow fuzz ------------------------


def test_fuzz_1000_calls_zero_context_overflow(migrated_engine: Any) -> None:
    """The acceptance: adversarially long tool outputs across 1,000+ gateway
    calls produce ZERO context-overflow errors. A guarding provider FAILS the
    test on any call whose prompt side exceeds the window minus the reserve.

    Two layers: (1) 960 pure assembly checks over randomized adversarial
    inputs (giant thread lines, giant summaries, tiny windows); (2) 20 real
    turns through the rig whose scripted tools return 100k-char outputs — the
    guard asserts on every one of the ~60 real gateway calls."""
    rng = random.Random(38)

    # Layer 1 — the assembly math under fire (fast, wide).
    calls = 0
    for _ in range(960):
        window = rng.choice([2_048, 4_096, 8_192, 32_768, 200_000])
        budget = ContextBudget.for_window(window)
        thread = [("word " * rng.randint(1, 4_000)) + f"line{i}" for i in range(rng.randint(0, 40))]
        instruction = "act " * rng.randint(1, 6_000)
        summary = "s " * rng.randint(0, 9_000)
        kept, _evicted = fit_turns(thread, budget.recent_turns // 2)
        parts = [
            fit_text(instruction, budget.task_instruction),
            fit_text(summary, budget.working_summary),
            *kept,
        ]
        prompt_side = sum(_COUNT(p) for p in parts)
        # Everything the runtime assembles from unbounded inputs must respect
        # its slot; the packet + system slots are bounded by construction.
        allowed = budget.task_instruction + budget.working_summary + budget.recent_turns // 2 + 8
        assert prompt_side <= allowed, f"overflow at window={window}"
        calls += 1

    # Layer 2 — real turns, adversarial tool results, a guarding provider.
    class GuardedLlm(ScriptedLlm):
        def __init__(self, script: Any, window: int) -> None:
            super().__init__(script)
            self.window = window
            self.checked = 0

        async def chat(self, req: Any) -> ChatResponse:
            budget = ContextBudget.for_window(self.window)
            prompt_tokens = sum(
                _COUNT(m.content)
                if isinstance(m.content, str)
                else _COUNT(json.dumps(m.model_dump()))
                for m in req.messages
            )
            # The wire request also carries the granted tool SCHEMAS.
            prompt_tokens += sum(_COUNT(json.dumps(t.model_dump())) for t in (req.tools or []))
            assert prompt_tokens <= budget.prompt_ceiling, (
                f"context overflow: {prompt_tokens} > {budget.prompt_ceiling}"
            )
            self.checked += 1
            return await super().chat(req)

    async def scenario() -> int:
        from titan.tools.service import ExecutionOutcome, ToolExecutionContext, ToolSpec

        pid = await _make_project(migrated_engine)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        checked = 0
        for _i in range(20):
            # Alternate between space-separated flood and an UNBROKEN hex run —
            # the class a naive span-count estimator bills as "1 token".
            if _i % 2 == 0:
                giant = "A " * rng.randint(25_000, 60_000)
            else:
                giant = "deadbeef" * rng.randint(8_000, 16_000)

            async def _blob(
                arguments: dict[str, Any], context: ToolExecutionContext, _g: str = giant
            ) -> ExecutionOutcome:
                return ExecutionOutcome(result={"blob": _g})

            script = {
                "agent.eng_backend": [
                    _resp(tool_calls=[_tool_call("code.blob")]),
                    _resp(tool_calls=[_tool_call("code.blob")]),
                    # An invalid output drives a self-critique round (more
                    # convo growth), then the corrected output lands.
                    _resp(text="not json at all"),
                    _resp(text=_turn_json([_progress_msg()])),
                ]
            }
            llm = GuardedLlm(script, window=32_768)
            runtime = build_runtime(Settings(), migrated_engine, redis=redis, llm=llm)
            # Guard against the RUNTIME'S actual window for this agent, not a
            # guessed constant (models.yaml is the source of truth).
            llm.window = runtime.binding.context_window("eng_backend") or DEFAULT_CONTEXT_WINDOW
            runtime.backoff_base_s = 0.0
            _grant_tool(runtime, "eng_backend", "code.blob")
            runtime.tool_registry.register(
                ToolSpec(
                    name="code.blob",
                    provider="builtin:test",
                    description="adversarially large output",
                    risk="read",
                    required_scopes=(),
                    input_schema={"type": "object", "additionalProperties": True},
                    output_schema={"type": "object", "additionalProperties": True},
                    executor=_blob,
                    timeout_s=5.0,
                )
            )
            result = await run_turn(runtime, pid, "eng_backend", _seed_msg(pid))
            assert result.outcome == "completed", result.outcome
            assert result.output_retries == 1  # the critique round really ran
            checked += llm.checked
        return checked

    real_calls = run_async(scenario)
    assert calls + real_calls >= 1_000
    assert real_calls >= 40  # every real gateway call was overflow-guarded


def _tool_call(name: str) -> Any:
    from titan.llm.service import ToolCall

    return ToolCall(id=f"tc-{uuid.uuid4().hex[:8]}", name=name, arguments={})


def _progress_msg() -> dict[str, Any]:
    return {
        "type": "ProgressReport",
        "to_agent": "pm_project",
        "payload": {"status": "in_progress", "percent_complete": 50, "summary": "ok"},
    }


def _decision_msg() -> dict[str, Any]:
    return {
        "type": "DecisionLog",
        "to_agent": "pm_project",
        "payload": {
            "decision": "Use uuidv7 keys",
            "context": "index bloat",
            "trade_offs": "migration cost",
            "authority": "backend",
            "reversible": True,
        },
    }


def _seed_msg(pid: uuid.UUID, task_id: uuid.UUID | None = None) -> AgentMessage:
    return AgentMessage(
        type="TaskAssignment",
        project_id=pid,
        task_id=task_id,
        from_agent="pm_project",
        to_agent="eng_backend",
        payload={
            "title": "Build the widget",
            "description": "Implement it",
            "acceptance_criteria": ["works"],
        },
    )


async def _make_task(engine: Any, pid: uuid.UUID) -> uuid.UUID:
    tid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tasks (id, project_id, title) VALUES (:id, :pid, 'wm test task')"),
            {"id": tid, "pid": pid},
        )
    return tid


# --- §4.5 keyspace + TITAN-BRN-016 summary schema -----------------------------------


def test_wm_keyspace_shapes_and_ttl() -> None:
    async def scenario() -> tuple[list[int], list[dict[str, Any]], int]:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        wm = WorkingMemory(redis)
        tid = uuid.uuid4()
        await wm.append_scratch(tid, 2, {"seq": 2})
        await wm.append_scratch(tid, 1, {"seq": 1})
        await wm.store_summary(tid, RollingSummary(goal="g"))
        await wm.queue_events(tid, [{"a": 1}, {"b": 2}])
        ttls = [
            await redis.ttl(f"wm:{tid}:scratch"),
            await redis.ttl(f"wm:{tid}:summary"),
            await redis.ttl(f"wm:{tid}:events"),
        ]
        scratch = await wm.scratch(tid)
        await wm.ack_events(tid, 1)
        return ttls, scratch, await wm.pending_count(tid)

    ttls, scratch, pending = run_async(scenario)
    # §4.5 TTLs on the re-derivable keys; the PENDING EVENTS list must NOT
    # expire (TITAN-BRN-002 — see test_pending_events_never_expire_scratch_does).
    assert all(0 < ttl <= 24 * 60 * 60 for ttl in ttls[:2])
    assert ttls[2] == -1
    assert [e["seq"] for e in scratch] == [1, 2]  # ordered by call seq
    assert pending == 1  # ack drops exactly the flushed prefix


def test_rolling_summary_five_field_schema_is_strict() -> None:
    with pytest.raises(ValidationError):
        RollingSummary.model_validate({"goal": "g", "surprise": "x"})  # extra key -> reject
    rendered = RollingSummary(goal="G", next_step="N").render()
    assert "Goal: G" in rendered and "Next step: N" in rendered
    assert RollingSummary().render() == ""


def test_summary_folder_parses_model_output_and_falls_back() -> None:
    class OneShotGateway:
        def __init__(self, text: str) -> None:
            self.text = text
            self.aliases: list[str | None] = []

        async def chat(self, req: Any) -> Any:
            self.aliases.append(req.model_alias)

            class _R:
                text = self.text

            return _R()

    async def scenario() -> tuple[RollingSummary, RollingSummary, list[str | None]]:
        good = OneShotGateway(
            json.dumps(
                {
                    "goal": "ship it",
                    "done_so_far": "built it",
                    "open_threads": "",
                    "constraints_discovered": "",
                    "next_step": "test it",
                }
            )
        )
        parsed = await SummaryFolder(good, "local-default").fold(
            RollingSummary(), ["turn one happened"]
        )
        garbage = OneShotGateway("i refuse to answer in JSON")
        fallback = await SummaryFolder(garbage, "local-default").fold(
            RollingSummary(goal="keep"), ["EVICTEDMARK turn detail"]
        )
        return parsed, fallback, good.aliases

    parsed, fallback, aliases = run_async(scenario)
    assert parsed.goal == "ship it" and parsed.next_step == "test it"
    # TITAN-BRN-017: the fold routed to the cheapest (utility) alias, never premium.
    assert aliases == ["local-default"]
    # The deterministic fallback keeps the old fields and the evicted material.
    assert fallback.goal == "keep"
    assert "EVICTEDMARK" in fallback.done_so_far


def test_checkpoint_survives_redis_flush(migrated_engine: Any) -> None:
    """TITAN-BRN-016/-002: fold -> checkpoint to PostgreSQL; flush Redis; the
    coordinator recovers the summary from the checkpoint."""

    class SilentGateway:
        async def chat(self, req: Any) -> Any:  # malformed -> deterministic fallback
            class _R:
                text = "no json here"

            return _R()

    async def scenario() -> tuple[RollingSummary, RollingSummary]:
        pid = await _make_project(migrated_engine)
        tid = await _make_task(migrated_engine, pid)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        coordinator = WorkingMemoryCoordinator(
            migrated_engine, WorkingMemory(redis), SummaryFolder(SilentGateway())
        )
        state = await coordinator.hydrate(tid)
        assert state.healthy and state.fold_count == 0 and state.folded_through == 0
        folded = await coordinator.fold(tid, state, ["CHECKPOINTMARK detail"], folded_through=1)
        await redis.flushdb()  # the §4.5 store is disposable
        recovered = await coordinator.hydrate(tid)
        assert recovered.fold_count == 1
        assert recovered.folded_through == 1  # the watermark survives too
        return folded, recovered.summary

    folded, recovered = run_async(scenario)
    assert "CHECKPOINTMARK" in folded.done_so_far
    assert recovered == folded  # byte-identical recovery from PostgreSQL


# --- TITAN-BRN-002: completion gate + Redis-flush replay invariance -----------------


def test_completion_rejected_while_wm_events_pending(api: dict[str, Any]) -> None:
    """The DoD acceptance: approve (in_review -> done) is 409-rejected while
    wm:{task_id}:events is non-empty, and succeeds once drained."""
    pid = _project(api)
    tid = _task(api, pid)
    assignee, reviewer = _agent(api, "backend"), _agent(api, "reviewer")
    _drive_to_in_review(api, tid, assignee)
    app_redis = api["client"].app.state.redis

    async def queue_event() -> None:
        await app_redis.rpush(events_key(uuid.UUID(tid)), json.dumps({"pending": True}))

    run_async(queue_event)
    blocked = _transition(api, tid, "approve", actor=reviewer)
    assert blocked.status_code == 409
    assert "TITAN-BRN-002" in blocked.json()["detail"]

    async def drain() -> None:
        await app_redis.delete(events_key(uuid.UUID(tid)))

    run_async(drain)
    assert _transition(api, tid, "approve", actor=reviewer).status_code == 200  # drained


def test_redis_flush_does_not_change_retrieval_only_replay(api: dict[str, Any]) -> None:
    """TITAN-BRN-002 acceptance: working memory is never the sole copy of
    anything durable — flushing Redis between two identical retrieval-only
    packet requests changes nothing but cache temperature."""
    engine = api["engine"]

    async def scenario() -> tuple[list[Any], list[Any]]:
        await quiesce_outbox(engine)
        pipeline = Pipeline(engine, embeddings=HashingEmbeddings())
        await pipeline.seed(
            [
                {"title": f"replay doc {i}", "body_md": f"REPLAYMARK{i:02d} durable fact"}
                for i in range(8)
            ]
        )
        first = await pipeline.packet(query="REPLAYMARK03 durable fact", scope="org")
        await pipeline.redis.flushdb()  # kill cache + any wm state
        second = await pipeline.packet(query="REPLAYMARK03 durable fact", scope="org")
        return list(first.citations), list(second.citations)

    first, second = run_async(scenario)
    assert [c.record_id for c in first] == [c.record_id for c in second]
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


# --- §13: the full turn write-back through the runtime -------------------------------


def test_turn_writes_back_scratch_events_ledger_and_kg(migrated_engine: Any) -> None:
    """One completed turn with a DecisionLog: the §13 write-back appends the
    scratch entry, flushes an EPISODIC project-scope Memory Event through the
    transactional write path (record + ledger row, actor = the agent), leaves
    wm:{task}:events EMPTY (the completion gate would pass), and feeds the
    typed no-op KG sink. Also: the packet_id is set (TITAN-BRN-009)."""

    async def scenario() -> dict[str, Any]:
        pid = await _make_project(migrated_engine)
        tid = await _make_task(migrated_engine, pid)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        script = {"agent.eng_backend": [_resp(text=_turn_json([_decision_msg(), _progress_msg()]))]}
        runtime = build_runtime(Settings(), migrated_engine, redis=redis, llm=ScriptedLlm(script))
        assert runtime.coordinator is not None
        kg = NoopKgSink()
        runtime.coordinator.kg = kg
        result = await run_turn(runtime, pid, "eng_backend", _seed_msg(pid, task_id=tid))
        assert result.outcome == "completed"
        wm = WorkingMemory(redis)
        async with migrated_engine.connect() as conn:
            records = (
                await conn.execute(
                    text(
                        "SELECT kind, scope, source_agent FROM brain.memory_records "
                        "WHERE source_task_id = :tid"
                    ),
                    {"tid": tid},
                )
            ).all()
            ledger = (
                await conn.execute(
                    text(
                        "SELECT actor_agent, action FROM brain.memory_ledger WHERE task_id = :tid"
                    ),
                    {"tid": tid},
                )
            ).all()
        return {
            "packet_id": result.packet_id,
            "records": records,
            "ledger": ledger,
            "pending": await wm.pending_count(tid),
            "scratch": await wm.scratch(tid),
            "kg": kg.accepted,
        }

    facts = run_async(scenario)
    assert facts["packet_id"] is not None  # TITAN-BRN-009: exactly one per call
    kinds = {(r.kind, r.scope.split(":")[0]) for r in facts["records"]}
    assert ("episodic", "project") in kinds  # the DecisionLog became durable memory
    assert all(r.scope.startswith("project:") for r in facts["records"])  # TITAN-BRN-024
    assert any(
        row.actor_agent == "agent.eng_backend" and row.action == "write" for row in facts["ledger"]
    )
    assert facts["pending"] == 0  # flushed before the call returned
    assert facts["scratch"] and facts["scratch"][0]["message_types"] == [
        "DecisionLog",
        "ProgressReport",
    ]
    assert facts["kg"] == 1  # the §13 step-11 hook fired (typed no-op)


def test_turn_uses_real_packet_provider(migrated_engine: Any) -> None:
    """The runtime's packet comes from the REAL Phase-36 pipeline: a fact
    seeded through memory lands in the turn's packet citations (one packet per
    call, cited, project-scoped)."""

    async def scenario() -> tuple[Any, int]:
        pid = await _make_project(migrated_engine)
        tid = await _make_task(migrated_engine, pid)
        await quiesce_outbox(migrated_engine)
        pipeline = Pipeline(migrated_engine, embeddings=HashingEmbeddings())
        await pipeline.seed(
            [{"title": "widget history", "body_md": "PROVIDERMARK the widget uses uuidv7 keys"}],
            scope=f"project:{pid}",
        )
        provider = PacketMemoryProvider(
            migrated_engine,
            pipeline.redis,
            pipeline.qdrant,
            pipeline.embeddings,
            CrossEncoderReranker(score_fn=overlap_scorer),
        )
        packet = await provider.assemble(
            MemoryRequest(
                agent_id="eng_backend",
                project_id=pid,
                task_id=tid,
                query="PROVIDERMARK widget keys",
                budget_tokens=4_000,
            )
        )
        return packet, len(packet.citations)

    packet, citations = run_async(scenario)
    assert citations >= 1
    assert "PROVIDERMARK" in packet.render()


# --- TITAN-BRN-024: org-scope writes from agent credentials --------------------------


def test_service_rejects_agent_org_scope_write(migrated_engine: Any) -> None:
    async def scenario() -> str:
        batch = memory_service.MemoryEventBatch(
            source_agent="agent.eng_backend",
            events=[
                memory_service.MemoryEvent(
                    kind="semantic", scope="org", title="sneaky", body_md="org write"
                )
            ],
        )
        try:
            async with migrated_engine.begin() as conn:
                await memory_service.write_events(
                    conn, batch, source_agent="agent.eng_backend", actor_agent="agent.eng_backend"
                )
        except Exception as exc:
            return str(exc)
        return "ALLOWED"

    detail = run_async(scenario)
    assert "TITAN-BRN-024" in detail and "403" in detail


def test_endpoint_scope_rules_for_humans(api: dict[str, Any]) -> None:
    """The HTTP surface: an org-privileged human still writes org scope (the
    curation/promotion path); the viewer role (no memory:write) is 403."""
    client, token_for = api["client"], api["token_for"]
    body = {
        "source_agent": "memory_agent",
        "events": [{"kind": "semantic", "scope": "org", "title": "t", "body_md": "b"}],
    }
    ok = client.post("/api/v1/memory/events", json=body, headers=token_for("admin"))
    assert ok.status_code == 201
    denied = client.post("/api/v1/memory/events", json=body, headers=token_for("viewer"))
    assert denied.status_code == 403


# --- §14: GET /memory/metrics ---------------------------------------------------------


def test_memory_metrics_endpoint(api: dict[str, Any]) -> None:
    response = api["client"].get("/api/v1/memory/metrics", headers=api["token_for"]("viewer"))
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"packet_cache", "ingest_lag"}
    assert set(payload["packet_cache"]) == {"hits", "misses", "hit_rate"}
    assert set(payload["ingest_lag"]) == {"count", "p50_seconds", "p95_seconds"}
    assert api["client"].get("/api/v1/memory/metrics").status_code == 401


# --- Round-1 review hardening: estimator, flush semantics, TTLs, metrics math ------


def test_count_tokens_bills_unbroken_runs_at_bpe_scale() -> None:
    """A 400k-char hex run is ~100k BPE tokens, not 1 — the class of tool
    output that must not ride through any budget as 'one token'."""
    blob = "deadbeef" * 50_000  # 400,000 unbroken word characters
    assert _COUNT(blob) == 100_000
    assert _COUNT("ordinary words stay unchanged") == 4
    assert _COUNT("ix_ingest_outbox_unprocessed") == 1  # long-ish identifiers: still 1
    # fit_text cuts INSIDE the run: the output is char-bounded like real BPE.
    out = fit_text(blob, 100)
    assert len(out) <= 100 * 4 + len(TRUNCATION_MARKER)
    assert _COUNT(out) <= 100


def test_flush_is_serialised_idempotent_and_poison_safe(migrated_engine: Any) -> None:
    """The at-least-once flush: concurrent flushers never double-write (the
    per-task lock), a crash-window re-flush dedups on wm_client_key, and a
    poison entry is dead-lettered instead of blocking the queue forever."""

    async def scenario() -> dict[str, Any]:
        pid = await _make_project(migrated_engine)
        tid = await _make_task(migrated_engine, pid)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        coordinator = WorkingMemoryCoordinator(
            migrated_engine, WorkingMemory(redis), SummaryFolder(object())
        )
        key = str(uuid.uuid4())
        event = {
            "kind": "episodic",
            "scope": f"project:{pid}",
            "title": "flush test",
            "body_md": "decision detail",
            "metadata": {"wm_client_key": key},
            "importance": 0.6,
            "source_task_id": str(tid),
        }
        wm = coordinator.wm
        # Poison first (JSON garbage), then the real event.
        await redis.rpush(events_key(tid), "{not json")
        await wm.queue_events(tid, [event])
        import asyncio

        first, second = await asyncio.gather(
            coordinator.flush_pending(tid, agent_id="eng_backend"),
            coordinator.flush_pending(tid, agent_id="eng_backend"),
        )
        # Crash-window replay: the SAME event queued and flushed again — the
        # deterministic client key dedups it to zero new records.
        await wm.queue_events(tid, [event])
        third = await coordinator.flush_pending(tid, agent_id="eng_backend")
        async with migrated_engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.memory_records "
                        "WHERE metadata ->> 'wm_client_key' = :k"
                    ),
                    {"k": key},
                )
            ).scalar_one()
        dead = await redis.lrange(f"{events_key(tid)}:dead", 0, -1)
        return {
            "written": sorted([first, second, third]),
            "durable": int(rows),
            "pending": await wm.pending_count(tid),
            "dead": len(dead),
        }

    facts = run_async(scenario)
    assert facts["durable"] == 1  # written exactly once across three flushes
    assert facts["written"] == [0, 0, 1]  # lock + dedup: one winner, two no-ops
    assert facts["pending"] == 0  # queue fully drained
    assert facts["dead"] == 1  # the poison entry quarantined, not lost, not blocking


def test_pending_events_never_expire_scratch_does(migrated_engine: Any) -> None:
    """TITAN-BRN-002 vs the §4.5 TTL table: the events list is the sole copy
    of pending durable memory — it must NOT expire; scratch/summary (both
    re-derivable) keep their 24 h TTL."""

    async def scenario() -> tuple[int, int, int]:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        wm = WorkingMemory(redis)
        tid = uuid.uuid4()
        await wm.append_scratch(tid, 1, {"seq": 1})
        await wm.store_summary(tid, RollingSummary(goal="g"))
        await wm.queue_events(tid, [{"pending": True}])
        return (
            await redis.ttl(f"wm:{tid}:scratch"),
            await redis.ttl(f"wm:{tid}:summary"),
            await redis.ttl(f"wm:{tid}:events"),
        )

    scratch_ttl, summary_ttl, events_ttl = run_async(scenario)
    assert 0 < scratch_ttl <= 24 * 60 * 60
    assert 0 < summary_ttl <= 24 * 60 * 60
    assert events_ttl == -1  # PERSISTed while pending


def test_memory_metrics_quantile_math() -> None:
    """The /memory/metrics histogram interpolation on a known distribution."""
    from titan.observability.metrics import MEMORY_INGEST_LAG

    for _ in range(50):
        MEMORY_INGEST_LAG.observe(0.11)
    for _ in range(50):
        MEMORY_INGEST_LAG.observe(3.0)
    out = memory_service.memory_metrics()
    assert out.ingest_lag.count >= 100
    assert out.ingest_lag.p50_seconds is not None and out.ingest_lag.p95_seconds is not None
    assert out.ingest_lag.p50_seconds < out.ingest_lag.p95_seconds
    assert out.ingest_lag.p95_seconds >= 1.0  # the slow half dominates the tail


# --- Round-2 review hardening: replay dedup, fold divergence, poison retries --------


def test_replay_reflush_writes_each_event_once(migrated_engine: Any) -> None:
    """R2 finding 1: the crash-window replay re-queues byte-equal events with
    the SAME wm_client_key, so ONE flush snapshot can hold two copies. Intra-
    batch dedup (not just DB-known dedup) must keep exactly one durable record
    — the index is not unique, so it is no backstop."""

    async def scenario() -> tuple[int, int]:
        pid = await _make_project(migrated_engine)
        tid = await _make_task(migrated_engine, pid)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        coordinator = WorkingMemoryCoordinator(
            migrated_engine, WorkingMemory(redis), SummaryFolder(object())
        )
        key = str(uuid.uuid4())
        event = {
            "kind": "episodic",
            "scope": f"project:{pid}",
            "title": "replay dedup",
            "body_md": "decision",
            "metadata": {"wm_client_key": key},
            "importance": 0.6,
            "source_task_id": str(tid),
        }
        # BOTH copies land in a single, not-yet-flushed snapshot (the exact
        # replay window: finish_turn queued once, replay_write_back queued the
        # byte-equal event again, nothing durable yet).
        await coordinator.wm.queue_events(tid, [event, event])
        written = await coordinator.flush_pending(tid, agent_id="eng_backend")
        async with migrated_engine.connect() as conn:
            durable = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.memory_records "
                        "WHERE metadata ->> 'wm_client_key' = :k"
                    ),
                    {"k": key},
                )
            ).scalar_one()
        return written, int(durable)

    written, durable = run_async(scenario)
    assert written == 1  # one of the two copies was deduped inside the batch
    assert durable == 1  # exactly one durable record


def test_hydrate_takes_content_and_watermark_from_the_same_store(migrated_engine: Any) -> None:
    """R2 finding 2: a fold that reaches PG but not Redis leaves the checkpoint
    with newer content+watermark while Redis holds a stale summary. hydrate
    must not pair the checkpoint's watermark with the stale Redis CONTENT (that
    would skip the folded lines forever) — the checkpoint is authoritative for
    both when it exists."""

    async def scenario() -> tuple[str, int, int, str]:
        pid = await _make_project(migrated_engine)
        tid = await _make_task(migrated_engine, pid)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        wm = WorkingMemory(redis)
        # PG checkpoint: the NEW folded summary at fold_count 3, watermark 5.
        new_summary = RollingSummary(goal="G", done_so_far="FOLDEDMARK detail")
        async with migrated_engine.begin() as conn:
            await memory_service.checkpoint_working_summary(
                conn, tid, new_summary, fold_count=3, folded_through=5
            )
        # Redis still holds the STALE pre-fold summary (the SET that would have
        # updated it failed): different content, and it carries no fold_count.
        await wm.store_summary(tid, RollingSummary(goal="G", done_so_far="STALE"))
        coordinator = WorkingMemoryCoordinator(migrated_engine, wm, SummaryFolder(object()))
        state = await coordinator.hydrate(tid)
        # Redis was reconciled to the checkpoint content.
        reconciled = await wm.load_summary(tid)
        return (
            state.summary.done_so_far,
            state.fold_count,
            state.folded_through,
            reconciled.done_so_far if reconciled else "",
        )

    content, fold_count, watermark, reconciled = run_async(scenario)
    assert content == "FOLDEDMARK detail"  # checkpoint content, NOT stale Redis
    assert fold_count == 3 and watermark == 5  # travelling together
    assert reconciled == "FOLDEDMARK detail"  # Redis healed to match


def test_poison_dead_lettered_once_across_write_failures(migrated_engine: Any) -> None:
    """R2 finding 3: dead-lettering happens on the acked path only, so a poison
    entry that shares a flush with a WRITE that first fails then succeeds is
    quarantined exactly once, not once per retry."""

    async def scenario() -> tuple[int, int]:
        pid = await _make_project(migrated_engine)
        tid = await _make_task(migrated_engine, pid)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        wm = WorkingMemory(redis)

        class FlakyEngine:
            """Wraps the real engine; the FIRST begin() raises, the rest pass —
            simulating a transient PG outage during the write."""

            def __init__(self, real: Any) -> None:
                self._real = real
                self.fail_next = True

            def connect(self) -> Any:
                return self._real.connect()

            def begin(self) -> Any:
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("transient PG outage (injected)")
                return self._real.begin()

        flaky = cast(Any, FlakyEngine(migrated_engine))
        coordinator = WorkingMemoryCoordinator(flaky, wm, SummaryFolder(object()))
        good = {
            "kind": "episodic",
            "scope": f"project:{pid}",
            "title": "poison retry",
            "body_md": "d",
            "metadata": {"wm_client_key": str(uuid.uuid4())},
            "importance": 0.6,
            "source_task_id": str(tid),
        }
        await redis.rpush(events_key(tid), "{not json")
        await wm.queue_events(tid, [good])
        first = await coordinator.flush_pending(tid, agent_id="eng_backend")  # write raises
        second = await coordinator.flush_pending(tid, agent_id="eng_backend")  # succeeds
        dead = await redis.lrange(f"{events_key(tid)}:dead", 0, -1)
        return len(dead), second + first

    dead_count, written = run_async(scenario)
    assert dead_count == 1  # quarantined exactly once despite the first failure
    assert written == 1  # the good event still became durable on the retry


def test_clamp_convo_compresses_bare_string_critique_messages() -> None:
    """R2b hardening: the self-critique path appends the model's invalid output
    as a BARE-STRING assistant message. Two near-max ones must not escape the
    conversation budget (TITAN-BRN-015) — rung 2 compresses them too."""
    from titan.llm.service import Message
    from titan.runtime.turn import _clamp_convo, _convo_cost

    big = "prose " * 4_000  # a large invalid output the model emitted
    convo = [
        Message(role="user", content="seed instruction"),
        Message(role="assistant", content=big),  # critique round 1
        Message(role="user", content="Your previous output was invalid: ..."),
        Message(role="assistant", content=big),  # critique round 2
        Message(role="user", content="Your previous output was invalid: ..."),
    ]
    assert _convo_cost(convo) > 500
    _clamp_convo(convo, 500)
    assert _convo_cost(convo) <= 500  # the bare-string assistant turns shrank
    assert convo[0].content == "seed instruction"  # seed untouched
