"""Scorecard generation + KPI rollup + degradation (Phase 84, Vol 10 §3) — DB tests.

Proves the DoD: a scorecard row for a terminal task with the rubric produced by a
DIFFERENT agent than the executor, linking task/agent/prompt_sha/model/trace; the
KPI rollup reconciles with the raw scorecards; and a seeded KPI drop opens an
investigation carrying the series snapshot.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.agents.service import build_evolution_resolver
from titan.db import uuid7
from titan.evolution import kpi as kpi_mod
from titan.evolution import repository as repo
from titan.evolution import scoring
from titan.llm.models import ChatResponse, Usage
from titan.settings import Settings

pytestmark = pytest.mark.schema

_DIMS = {
    "correctness": 90,
    "first_pass_quality": 88,
    "rework_ratio": 85,
    "efficiency": 80,
    "cost": 82,
    "standards_compliance": 91,
    "security": 87,
    "collaboration": 84,
}


class _DimsGateway:
    """A mock gateway whose rubric call returns fixed dimensions and records the
    agent_id it was asked to evaluate as (to prove non-executor scoring)."""

    def __init__(self, dims: dict[str, int]) -> None:
        self._dims = dims
        self.called_agent: str | None = None

    async def chat(self, req: Any) -> ChatResponse:
        self.called_agent = req.metadata.agent_id
        return ChatResponse(
            text=json.dumps(self._dims),
            finish_reason="stop",
            usage=Usage(input_tokens=10, output_tokens=20),
            provider="mock",
            model_id="mock/eval",
            cost_usd=Decimal("0"),
            latency_ms=1,
        )


async def _seed_project_task(
    engine: Any,
    *,
    role: str = "backend",
    status: str = "done",
    cost: str = "0.20",
    tokens_in: int = 3000,
    tokens_out: int = 1500,
    attempt: int = 1,
    with_run: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    pid, tid, aid, uid, rid = uuid7(), uuid7(), uuid7(), uuid7(), uuid7()
    slug = f"evo-{pid.hex[:10]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :email, 'evo', 'local', :sub)"
            ),
            {"id": uid, "email": f"{uid}@t.local", "sub": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, mode, created_by) "
                "VALUES (:id, :name, 'Evo', 'brief', 'autonomous', :uid)"
            ),
            {"id": pid, "name": slug, "uid": uid},
        )
        await conn.execute(
            text(
                "INSERT INTO agents (id, project_id, role, name, model_binding, config) "
                "VALUES (:id, :pid, :role, 'exec', CAST('{}' AS JSONB), CAST('{}' AS JSONB))"
            ),
            {"id": aid, "pid": pid, "role": role},
        )
        await conn.execute(
            text(
                "INSERT INTO tasks (id, project_id, title, spec, status, priority, "
                "assignee_agent_id) VALUES (:id, :pid, 'build widget', 'do it', :status, 3, :aid)"
            ),
            {"id": tid, "pid": pid, "status": status, "aid": aid},
        )
        if with_run:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, task_id, agent_id, attempt, status, input, model, "
                    "started_at, finished_at) VALUES (:id, :tid, :aid, :attempt, 'succeeded', "
                    "CAST('{}' AS JSONB), 'anthropic/claude', :start, :finish)"
                ),
                {
                    "id": rid,
                    "tid": tid,
                    "aid": aid,
                    "attempt": attempt,
                    "start": datetime.now(UTC) - timedelta(seconds=120),
                    "finish": datetime.now(UTC),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO llm_usage (id, project_id, task_id, agent_id, served_alias, "
                    "provider, model, input_tokens, output_tokens, cost_usd, latency_ms) "
                    "VALUES (:id, :pid, :tid, 'agent.backend', 'primary', 'anthropic', "
                    "'anthropic/claude', :ti, :to, :cost, 100)"
                ),
                {
                    "id": uuid7(),
                    "pid": pid,
                    "tid": tid,
                    "ti": tokens_in,
                    "to": tokens_out,
                    "cost": Decimal(cost),
                },
            )
    return pid, tid


async def _seed_scorecard(
    engine: Any,
    project_id: uuid.UUID,
    *,
    agent: str,
    task_class: str,
    composite: float,
    created_at: datetime,
) -> None:
    # A fresh task per scorecard — uq_agent_scorecards_task enforces one per task.
    task_id = uuid7()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tasks (id, project_id, title, spec, status, priority) "
                "VALUES (:id, :pid, 'sc', '', 'done', 3)"
            ),
            {"id": task_id, "pid": project_id},
        )
        await conn.execute(
            text(
                "INSERT INTO agent_scorecards (id, task_id, agent_id, agent_spec_sha, prompt_sha, "
                "model_id, task_class, dimensions, composite_score, cost_usd, tokens_in, "
                "tokens_out, wall_clock_s, trace_id, created_at) VALUES (:id,:tid,:agent,'s','p',"
                "'m',:cls,CAST(:dims AS JSONB),:comp,0,1,1,1,'t',:at)"
            ),
            {
                "id": uuid7(),
                "tid": task_id,
                "agent": agent,
                "cls": task_class,
                "dims": json.dumps({d: int(composite) for d in scoring.DIMENSIONS}),
                "comp": composite,
                "at": created_at,
            },
        )


def test_terminal_transition_emits_task_terminal(migrated_engine: Any) -> None:
    """The ONE terminal transition emits task.terminal (the scorecard trigger)."""
    import fakeredis.aioredis

    from titan.orchestrator import service as orch
    from titan.orchestrator import transitions

    async def scenario() -> None:
        pid, uid = uuid7(), uuid7()
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :e, 'e', 'local', :s)"
                ),
                {"id": uid, "e": f"{uid}@t.local", "s": str(uid)},
            )
            await conn.execute(
                text(
                    "INSERT INTO projects (id, name, display_name, brief, mode, created_by) "
                    "VALUES (:id, :n, 'E', 'b', 'autonomous', :u)"
                ),
                {"id": pid, "n": f"evo-{pid.hex[:10]}", "u": uid},
            )
            task = await orch.create_task(
                conn, pid, orch.TaskCreate(title="cancel me"), actor=str(uid)
            )
            trans = transitions.resolve(task.status, "cancel")
            assert trans is not None
            updated = await orch.apply_transition(
                conn,
                task,
                trans,
                actor_agent_id=None,
                actor_user_id=uid,
                comment=None,
                redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
            )
            assert updated.status == "cancelled"
            rows = (await conn.execute(text("SELECT payload FROM outbox"))).all()
        # The {type, task} event is nested in the outbox envelope; a serialized
        # substring check is robust to the envelope shape.
        blob = " ".join(json.dumps(p) if not isinstance(p, str) else p for (p,) in rows)
        assert '"task.terminal"' in blob

    run_async(scenario)


def test_generate_scorecard_links_and_uses_a_non_executor(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid, tid = await _seed_project_task(migrated_engine, role="backend", status="done")
        gateway = _DimsGateway(_DIMS)
        resolver = build_evolution_resolver(Settings())
        row = await scoring.generate_scorecard(migrated_engine, gateway, resolver, task_id=tid)
        assert row is not None
        # Rubric produced by a DIFFERENT agent than the executor (backend→reviewer).
        assert gateway.called_agent == "agent.reviewer"
        assert row["agent_id"] == "eng_backend" and row["task_class"] == "code"
        # Links task/agent/prompt_sha/model/trace, all non-empty.
        assert row["task_id"] == str(tid)
        assert row["prompt_sha"] == resolver.manifest.prompts["eng_backend"].sha256
        assert row["model_id"] == "anthropic/claude"
        assert row["trace_id"] and row["agent_spec_sha"] != "unknown"
        assert set(row["dimensions"]) == set(scoring.DIMENSIONS)
        # Idempotent: a second call does not create a duplicate.
        assert (
            await scoring.generate_scorecard(migrated_engine, gateway, resolver, task_id=tid)
            is None
        )

    run_async(scenario)


def test_failed_task_caps_correctness(migrated_engine: Any) -> None:
    async def scenario() -> None:
        _, tid = await _seed_project_task(migrated_engine, role="backend", status="failed")
        gateway = _DimsGateway({**_DIMS, "correctness": 100})
        resolver = build_evolution_resolver(Settings())
        row = await scoring.generate_scorecard(migrated_engine, gateway, resolver, task_id=tid)
        assert row is not None and row["dimensions"]["correctness"] <= 30

    run_async(scenario)


def test_scorecard_written_even_when_rubric_evaluator_raises(migrated_engine: Any) -> None:
    """DoD/M5: a wonky evaluator (or any failure) must still yield a scorecard with
    conservative fallback dims — the 100%-of-terminal-tasks contract."""

    class _Boom:
        async def chat(self, req: Any) -> Any:
            raise RuntimeError("provider down")

    async def scenario() -> None:
        _, tid = await _seed_project_task(migrated_engine, role="backend", status="done")
        resolver = build_evolution_resolver(Settings())
        row = await scoring.generate_scorecard(migrated_engine, _Boom(), resolver, task_id=tid)
        assert row is not None  # still written despite the rubric failure
        assert set(row["dimensions"]) == set(scoring.DIMENSIONS)

    run_async(scenario)


def test_baseline_excludes_recent_so_a_real_drop_is_not_masked(migrated_engine: Any) -> None:
    """H2: a diluted 30d baseline (including the tanking recent rows) would hide a
    real drop; the fixed baseline (3,30] catches it."""

    async def scenario() -> None:
        pid, _ = await _seed_project_task(migrated_engine, with_run=False)
        agent = f"agent_{uuid.uuid4().hex[:8]}"
        for _ in range(5):  # baseline @90, 15 days ago (outside the recent window)
            await _seed_scorecard(
                migrated_engine,
                pid,
                agent=agent,
                task_class="code",
                composite=90.0,
                created_at=datetime.now(UTC) - timedelta(days=15),
            )
        for _ in range(10):  # a large recent cohort @78 — would dilute a naive baseline
            await _seed_scorecard(
                migrated_engine,
                pid,
                agent=agent,
                task_class="code",
                composite=78.0,
                created_at=datetime.now(UTC),
            )
        # Naive baseline (all 15 rows) = 82 → drop 4.9% (masked). Fixed baseline
        # (the 5 old rows) = 90 → drop 13.3% > 10% → detected.
        degradations = await kpi_mod.scan_degradations(migrated_engine)
        mine = [d for d in degradations if d["agent_id"] == agent]
        assert mine and mine[0]["drop_pct"] > 10.0

    run_async(scenario)


def test_single_recent_scorecard_does_not_open_investigation(migrated_engine: Any) -> None:
    """M1: one unlucky recent task must not open an investigation (needs a recent
    minimum sample count, not just a baseline one)."""

    async def scenario() -> None:
        pid, _ = await _seed_project_task(migrated_engine, with_run=False)
        agent = f"agent_{uuid.uuid4().hex[:8]}"
        for _ in range(8):
            await _seed_scorecard(
                migrated_engine,
                pid,
                agent=agent,
                task_class="code",
                composite=90.0,
                created_at=datetime.now(UTC) - timedelta(days=15),
            )
        await _seed_scorecard(  # a SINGLE recent low score
            migrated_engine,
            pid,
            agent=agent,
            task_class="code",
            composite=10.0,
            created_at=datetime.now(UTC),
        )
        degradations = await kpi_mod.scan_degradations(migrated_engine)
        assert not [d for d in degradations if d["agent_id"] == agent]

    run_async(scenario)


def test_kpi_rollup_reconciles_with_raw_scorecards(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid, _ = await _seed_project_task(migrated_engine, with_run=False)
        agent = f"agent_{uuid.uuid4().hex[:8]}"
        composites = [60.0, 80.0, 100.0]
        for c in composites:
            await _seed_scorecard(
                migrated_engine,
                pid,
                agent=agent,
                task_class="code",
                composite=c,
                created_at=datetime.now(UTC) - timedelta(days=1),
            )
        rollup = await kpi_mod.compute_rollup(migrated_engine)
        stats = rollup[7][(agent, "code")]
        assert stats["count"] == 3
        expected = sum(composites) / len(composites)  # 80.0
        assert abs(stats["composite"] - expected) <= expected * 0.005  # ±0.5%

    run_async(scenario)


def test_degradation_opens_investigation_with_snapshot(migrated_engine: Any) -> None:
    async def scenario() -> None:
        pid, _ = await _seed_project_task(migrated_engine, with_run=False)
        agent = f"agent_{uuid.uuid4().hex[:8]}"
        # Baseline: 8 high scorecards ~15 days ago (inside 30d, outside 3d).
        for _ in range(8):
            await _seed_scorecard(
                migrated_engine,
                pid,
                agent=agent,
                task_class="code",
                composite=90.0,
                created_at=datetime.now(UTC) - timedelta(days=15),
            )
        # Recent: 3 low scorecards now (inside 3d) — a sustained drop.
        for _ in range(3):
            await _seed_scorecard(
                migrated_engine,
                pid,
                agent=agent,
                task_class="code",
                composite=45.0,
                created_at=datetime.now(UTC),
            )
        degradations = await kpi_mod.scan_degradations(migrated_engine)
        mine = [d for d in degradations if d["agent_id"] == agent]
        assert mine and mine[0]["drop_pct"] > 10.0
        investigations = await repo.list_investigations(migrated_engine, status="open")
        opened = [i for i in investigations if i["agent_id"] == agent]
        assert opened and opened[0]["series_snapshot"]["recent"]["count"] == 3
        # Idempotent: a re-scan does not open a second investigation.
        await kpi_mod.scan_degradations(migrated_engine)
        again = [
            i
            for i in await repo.list_investigations(migrated_engine, status="open")
            if i["agent_id"] == agent
        ]
        assert len(again) == 1

    run_async(scenario)
