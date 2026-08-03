"""Post-task reflections + post-project retrospectives (Phase 85, Vol 10 §4).

Proves the DoD: after a demo task + project, the executing agent's reflection and
the PM's retrospective are retrievable through the *real* Memory Packet pipeline
(lexical + dense legs, byte-identical to production) with the correct kind
(episodic reflection at project scope; semantic retrospective at org scope) and a
citation resolving to the written record. Both are fail-safe and idempotent.
"""

import json
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from tests.memory_harness import Pipeline, quiesce_outbox
from tests.test_evolution_scorecard import _seed_project_task
from titan.agents.service import build_evolution_resolver
from titan.llm.models import ChatResponse, Usage
from titan.memory.ingest import EMBEDDERS_GROUP, STREAM_MEMORY_INGEST, process_ingest_batch
from titan.settings import Settings

pytestmark = pytest.mark.schema

_REFL_MARK = "REFLMARKERTOK"
_RETRO_MARK = "RETROMARKERTOK"


class _JsonGateway:
    """Returns a fixed JSON body as the model response (the reflection/retro shape)."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def chat(self, req: Any) -> ChatResponse:
        return ChatResponse(
            text=json.dumps(self._payload),
            finish_reason="stop",
            usage=Usage(input_tokens=8, output_tokens=40),
            provider="mock",
            model_id="mock/eval",
            cost_usd=Decimal("0"),
            latency_ms=1,
        )


async def _drain_ingest(pipe: Pipeline) -> None:
    """Run the real relay + embedder loop so both retrieval legs are populated."""
    await pipe.qdrant.ensure_collections()
    await pipe.bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
    for _ in range(60):
        published = await pipe.relay.relay_once()
        handled = await process_ingest_batch(pipe.embedder, pipe.bus, "pkt", block_ms=1)
        if not published and not handled:
            break


def test_reflection_and_retrospective_retrievable_via_packet(migrated_engine: Any) -> None:
    from titan.evolution import reflections

    async def scenario() -> None:
        pipe = Pipeline(migrated_engine, mode="on")
        resolver = build_evolution_resolver(Settings())
        # A demo task + project (cost > 0 so the reflection is not cost-capped).
        pid, tid = await _seed_project_task(
            migrated_engine, role="backend", status="done", cost="0.20"
        )
        # Isolate our ingest from other tests' pending outbox rows on the shared DB.
        await quiesce_outbox(migrated_engine)

        refl_gw = _JsonGateway(
            {
                "lesson": f"{_REFL_MARK} the DB pool starved under load during the widget build",
                "went_well": ["scoped the query"],
                "went_poorly": ["missed a pool cap"],
                "proposed_change": {"target": "eng_backend"},
                "confidence": 0.7,
            }
        )
        refl = await reflections.generate_post_task_reflection(
            migrated_engine,
            refl_gw,
            resolver,
            task_id=tid,
            project_id=pid,
            agent_roster="eng_backend",
            scorecard_id="sc-demo",
            context="build the widget",
            task_cost_usd=0.20,
        )
        assert refl is not None and refl["lesson"].startswith(_REFL_MARK)

        retro_gw = _JsonGateway(
            {
                "kpi_summary": f"{_RETRO_MARK} mean composite 82 across the project",
                "top_lessons": ["cap the pool"],
                "failure_inventory": [],
                "reuse_candidates": ["widget scaffolder"],
                "proposed_eps": [],
            }
        )
        retro = await reflections.generate_retrospective(
            migrated_engine, retro_gw, resolver, project_id=pid
        )
        assert retro is not None and retro["kpi_summary"].startswith(_RETRO_MARK)
        # Idempotent: one retrospective per project.
        assert (
            await reflections.generate_retrospective(
                migrated_engine, retro_gw, resolver, project_id=pid
            )
            is None
        )

        await _drain_ingest(pipe)

        # Resolve the two record ids for the citation assertions.
        async with migrated_engine.connect() as conn:
            refl_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM brain.memory_records WHERE scope = :scope AND "
                        "metadata->>'record_subtype' = 'post_task_reflection'"
                    ),
                    {"scope": f"project:{pid}"},
                )
            ).scalar()
            retro_id = (
                await conn.execute(
                    text(
                        "SELECT id FROM brain.memory_records WHERE scope = 'org' AND "
                        "metadata->>'record_subtype' = 'retrospective' AND "
                        "metadata->>'project_id' = :pid"
                    ),
                    {"pid": str(pid)},
                )
            ).scalar()
        assert refl_id is not None and retro_id is not None

        # The reflection: episodic, project scope, cited.
        rp = await pipe.packet(
            query=_REFL_MARK,
            scope=f"project:{pid}",
            kinds=["episodic", "semantic", "procedural"],
        )
        assert any(_REFL_MARK in item for item in rp.sections.episodic)
        assert any(str(c.record_id) == str(refl_id) for c in rp.citations)

        # The retrospective: semantic, org scope, cited.
        rq = await pipe.packet(
            query=_RETRO_MARK, scope="org", kinds=["episodic", "semantic", "procedural"]
        )
        assert any(_RETRO_MARK in item for item in rq.sections.semantic)
        assert any(str(c.record_id) == str(retro_id) for c in rq.citations)

    run_async(scenario)


def test_reflection_is_idempotent_across_redelivery(migrated_engine: Any) -> None:
    """A task.terminal redelivery re-runs the reflection step (the scorecard already
    exists); it must not write a duplicate reflection record."""
    from titan.evolution import reflections

    async def scenario() -> None:
        resolver = build_evolution_resolver(Settings())
        pid, tid = await _seed_project_task(
            migrated_engine, role="backend", status="done", cost="0.20"
        )
        gw = _JsonGateway({"lesson": "the one lesson", "confidence": 0.5})
        first = await reflections.generate_post_task_reflection(
            migrated_engine,
            gw,
            resolver,
            task_id=tid,
            project_id=pid,
            agent_roster="eng_backend",
            scorecard_id="sc",
            context="c",
            task_cost_usd=0.2,
        )
        assert first is not None
        second = await reflections.generate_post_task_reflection(
            migrated_engine,
            gw,
            resolver,
            task_id=tid,
            project_id=pid,
            agent_roster="eng_backend",
            scorecard_id="sc",
            context="c",
            task_cost_usd=0.2,
        )
        assert second is None  # deduped
        async with migrated_engine.connect() as conn:
            n = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.memory_records WHERE source_task_id = :tid "
                        "AND metadata->>'record_subtype' = 'post_task_reflection'"
                    ),
                    {"tid": tid},
                )
            ).scalar()
        assert n == 1

    run_async(scenario)


def test_reflection_skipped_for_zero_cost_task(migrated_engine: Any) -> None:
    """The 2% reflection cost cap is meaningless for a zero-LLM-cost task (§4) —
    skip it rather than spend a call."""
    from titan.evolution import reflections

    async def scenario() -> None:
        resolver = build_evolution_resolver(Settings())
        pid, tid = await _seed_project_task(migrated_engine, role="backend", status="done")
        called = {"n": 0}

        class _Counter:
            async def chat(self, req: Any) -> Any:
                called["n"] += 1
                raise AssertionError("must not be called for a zero-cost task")

        out = await reflections.generate_post_task_reflection(
            migrated_engine,
            _Counter(),
            resolver,
            task_id=tid,
            project_id=pid,
            agent_roster="eng_backend",
            scorecard_id=None,
            context="x",
            task_cost_usd=0.0,
        )
        assert out is None and called["n"] == 0

    run_async(scenario)
