"""Phase 40 — decay/curation + learning loops (Vol 5 §9/§11/§12,
TITAN-BRN-018/-019/-020/-023/-024/-025).

Covers the DoD:
- seeding records with back-dated timestamps + access counts, the curation job
  (dry-run plan, then apply) produces exactly the expected Demote/Archive/Compact
  transitions with matching ledger rows, while protected records at decay 0 are
  untouched;
- a task reflection lands as an episodic record and is retrieved in the next
  Memory Packet;
- a direct org-scope write with agent credentials is rejected;
- auto-restore flips a demoted/archived record back on access;
- lessons, pattern mining, and promotion; every new endpoint (happy + 403 +
  validation) and the done-block hook.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from tests.memory_fakes import FakeQdrantServer, StubEmbeddings
from tests.test_runtime_turn import _make_project
from titan.db import uuid7
from titan.memory import curation, patterns
from titan.memory import repository as repo
from titan.memory import service as memory
from titan.memory.qdrant import QdrantIndex

pytestmark = pytest.mark.schema


def _api_project(api: dict[str, Any]) -> str:
    """A real project via the API — its id backs the project_id FK on records
    written through the reflection/lesson endpoints."""
    name = f"cur-{uuid.uuid4().hex[:10]}"
    r = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


# --- helpers -----------------------------------------------------------------


async def _seed_record(
    engine: AsyncEngine,
    *,
    kind: str = "episodic",
    scope: str = "org",
    importance: float = 0.1,
    access_count: int = 0,
    age_days: int = 300,
    accessed_days: int | None = None,
    low_days: int | None = None,
    metadata: dict[str, Any] | None = None,
    status: str = "active",
    demoted: bool = False,
    title: str | None = None,
    project_id: uuid.UUID | None = None,
    with_chunk: bool = True,
) -> uuid.UUID:
    """Insert a record with precisely controlled decay inputs (back-dated
    created/accessed times, access count, importance, archive clock)."""
    rid = uuid7()
    ttl = title or f"seed-{rid.hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO brain.memory_records "
                "(id, kind, scope, project_id, source_agent, title, body_md, metadata, importance, "
                " access_count, status, demoted, created_at) "
                "VALUES (:id, :kind, :scope, :pid, 'seed', :title, :body, CAST(:meta AS jsonb), "
                " :imp, :ac, :status, :demoted, now() - make_interval(days => :age))"
            ),
            {
                "id": rid,
                "kind": kind,
                "scope": scope,
                "pid": project_id,
                "title": ttl,
                "body": f"body of {ttl} with distinctive content",
                "meta": json.dumps(metadata or {}),
                "imp": importance,
                "ac": access_count,
                "status": status,
                "demoted": demoted,
                "age": age_days,
            },
        )
        if accessed_days is not None:
            await conn.execute(
                text(
                    "UPDATE brain.memory_records SET last_accessed_at = "
                    "now() - make_interval(days => :d) WHERE id = :id"
                ),
                {"d": accessed_days, "id": rid},
            )
        if low_days is not None:
            await conn.execute(
                text(
                    "UPDATE brain.memory_records SET low_decay_since = "
                    "now() - make_interval(days => :d) WHERE id = :id"
                ),
                {"d": low_days, "id": rid},
            )
        if with_chunk:
            await conn.execute(
                text(
                    "INSERT INTO brain.memory_chunks (id, record_id, seq, text, token_count) "
                    "VALUES (:c, :r, 0, :t, 1)"
                ),
                {"c": uuid7(), "r": rid, "t": f"chunk of {ttl}"},
            )
    return rid


async def _make_task(engine: AsyncEngine, pid: uuid.UUID) -> uuid.UUID:
    tid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO tasks (id, project_id, title) VALUES (:id, :pid, 'reflection task')"),
            {"id": tid, "pid": pid},
        )
    return tid


def _task_via_engine(api: dict[str, Any], project_id: str) -> str:
    """A real task under a project (its id backs the source_task_id FK)."""
    tid = uuid.uuid4()

    async def make() -> None:
        async with api["engine"].begin() as conn:
            await conn.execute(
                text("INSERT INTO tasks (id, project_id, title) VALUES (:id, :pid, 'reflection')"),
                {"id": tid, "pid": uuid.UUID(project_id)},
            )

    run_async(make)
    return str(tid)


async def _record(engine: AsyncEngine, rid: uuid.UUID) -> Any:
    async with engine.connect() as conn:
        return await repo.get_record(conn, rid)


async def _ledger_tiers(engine: AsyncEngine, rid: uuid.UUID) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT detail ->> 'tier' AS tier FROM brain.memory_ledger "
                "WHERE :rid = ANY(record_ids) AND action = 'prune'"
            ),
            {"rid": rid},
        )
    return [r.tier for r in rows.all()]


class _CannedGateway:
    """A gateway stub whose chat() returns a fixed JSON body (reflection/miner)."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def chat(self, request: Any) -> Any:
        class _Resp:
            text = self._text

        return _Resp()


# --- §9.1 decay scoring ------------------------------------------------------


def test_decay_score_matches_formula(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> tuple[float, float]:
        # episodic (H=30), 60 days since access, access_count 10, importance 0.5.
        rid = await _seed_record(
            engine, kind="episodic", importance=0.5, access_count=10, accessed_days=60
        )
        async with engine.begin() as conn:
            await repo.recompute_decay_scores(conn)
        row = await _record(engine, rid)
        import math

        expected = 0.5 * math.exp(-60 / 30) + 0.3 * min(1.0, 10 / 20) + 0.2 * 0.5
        return float(row.decay_score), expected

    got, expected = run_async(scenario)
    assert abs(got - expected) < 0.01


# --- §9.2 tiers: dry-run then apply ------------------------------------------


def test_curation_dry_run_then_apply(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        # A demote candidate: old but importance/access keep it in [0.15, 0.35).
        demote = await _seed_record(
            engine, importance=0.8, access_count=5, accessed_days=300, title="demote-me"
        )
        # An archive candidate: near-zero decay AND low for > 30 days.
        archive = await _seed_record(
            engine, importance=0.05, access_count=0, age_days=400, low_days=45, title="archive-me"
        )
        # A healthy record: recently accessed → high decay → untouched.
        healthy = await _seed_record(
            engine, importance=0.9, access_count=15, accessed_days=1, title="healthy"
        )
        # Dry-run: plan only, nothing changes.
        plan = await memory.run_curation(engine, actor_agent="memory_agent", dry_run=True)
        demote_after_dry = await _record(engine, demote)
        # Apply.
        applied = await memory.run_curation(engine, actor_agent="memory_agent", dry_run=False)
        return {
            "plan": plan,
            "applied": applied,
            "demote_after_dry": demote_after_dry,
            "demote": await _record(engine, demote),
            "archive": await _record(engine, archive),
            "healthy": await _record(engine, healthy),
            "demote_tiers": await _ledger_tiers(engine, demote),
            "archive_tiers": await _ledger_tiers(engine, archive),
        }

    f = run_async(scenario)
    # Dry-run planned but changed nothing.
    assert f["plan"].dry_run is True and f["plan"].demoted == 0
    assert f["plan"].planned_demote >= 1 and f["plan"].planned_archive >= 1
    assert f["demote_after_dry"].demoted is False
    # Apply produced the exact transitions.
    assert f["demote"].demoted is True and f["demote"].status == "active"
    assert f["archive"].status == "archived"
    assert f["healthy"].demoted is False and f["healthy"].status == "active"
    # Ledger rows exist for each applied transition (TITAN-BRN-018).
    assert "demote" in f["demote_tiers"]
    assert "archive" in f["archive_tiers"]


def test_protected_records_untouched_at_zero_decay(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        # All maximally decayed (old, no access, low importance, archive clock old)
        # but each is a PROTECTED class → must survive.
        lesson = await _seed_record(
            engine, age_days=999, low_days=99, importance=0.0, metadata={"record_subtype": "lesson"}
        )
        decision = await _seed_record(
            engine,
            age_days=999,
            low_days=99,
            importance=0.0,
            metadata={"message_type": "DecisionLog"},
        )
        kb = await _seed_record(
            engine, age_days=999, low_days=99, importance=0.0, metadata={"kb_slug": "some-std"}
        )
        await memory.run_curation(engine, actor_agent="memory_agent", dry_run=False)
        return {
            "lesson": await _record(engine, lesson),
            "decision": await _record(engine, decision),
            "kb": await _record(engine, kb),
        }

    f = run_async(scenario)
    for key in ("lesson", "decision", "kb"):
        assert f[key].status == "active", key
        assert f[key].demoted is False, key


def test_kg_referenced_record_is_protected(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> Any:
        rid = await _seed_record(
            engine, age_days=999, low_days=99, importance=0.0, title="kg-backed"
        )
        scope = f"project:{uuid.uuid4()}"
        async with engine.begin() as conn:
            # A node backed by the record, with an active edge → protected.
            node = await conn.execute(
                text(
                    "INSERT INTO brain.kg_nodes (node_type, scope, name, record_id) "
                    "VALUES ('lesson', :s, 'n', :rid) RETURNING id"
                ),
                {"s": scope, "rid": rid},
            )
            nid = node.scalar_one()
            other = await conn.execute(
                text(
                    "INSERT INTO brain.kg_nodes (node_type, scope, name) "
                    "VALUES ('project', :s, 'p') RETURNING id"
                ),
                {"s": scope},
            )
            await conn.execute(
                text(
                    "INSERT INTO brain.kg_edges (src, dst, edge_type) "
                    "VALUES (:a, :b, 'DERIVED_FROM')"
                ),
                {"a": nid, "b": other.scalar_one()},
            )
        await memory.run_curation(engine, actor_agent="memory_agent", dry_run=False)
        return await _record(engine, rid)

    row = run_async(scenario)
    assert row.status == "active" and row.demoted is False


def test_archive_enqueues_vector_delete(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> int:
        archive = await _seed_record(
            engine, importance=0.0, access_count=0, age_days=400, low_days=45
        )
        await memory.run_curation(engine, actor_agent="memory_agent", dry_run=False)
        async with engine.connect() as conn:
            n = await conn.execute(
                text(
                    "SELECT count(*) FROM brain.ingest_outbox o JOIN brain.memory_chunks c "
                    "ON c.id = o.chunk_id WHERE c.record_id = :rid AND o.op = 'delete'"
                ),
                {"rid": archive},
            )
        return int(n.scalar_one())

    assert run_async(scenario) >= 1  # the pipeline drops the vectors (TITAN-BRN-018)


def test_compact_moves_body_to_cold(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        # Archived episodic older than 180 days → Compact candidate.
        rid = await _seed_record(
            engine, kind="episodic", status="archived", age_days=200, title="ancient"
        )
        await memory.run_curation(engine, actor_agent="memory_agent", dry_run=False)
        async with engine.connect() as conn:
            rec = await repo.get_record(conn, rid)
            cold = await conn.execute(
                text("SELECT body_md FROM brain.memory_bodies_cold WHERE record_id = :rid"),
                {"rid": rid},
            )
        return {"rec": rec, "cold": cold.scalar_one_or_none()}

    f = run_async(scenario)
    assert f["rec"].body_md.startswith("[compacted]")  # live body is the summary line
    assert f["cold"] is not None and "distinctive content" in f["cold"]  # full body preserved


# --- TITAN-BRN-020 auto-restore ----------------------------------------------


def test_access_restores_demoted_and_archived(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        demoted = await _seed_record(engine, demoted=True, importance=0.0, age_days=400)
        archived = await _seed_record(engine, status="archived", importance=0.0, age_days=400)
        # Access each through the read path (get_record_detail restores).
        async with engine.begin() as conn:
            await memory.get_record_detail(conn, demoted, actor_agent="viewer", task_id=None)
        async with engine.begin() as conn:
            await memory.get_record_detail(conn, archived, actor_agent="viewer", task_id=None)
        async with engine.connect() as conn:
            reembed = await conn.execute(
                text(
                    "SELECT count(*) FROM brain.ingest_outbox o JOIN brain.memory_chunks c "
                    "ON c.id = o.chunk_id WHERE c.record_id = :rid AND o.op = 'upsert'"
                ),
                {"rid": archived},
            )
        return {
            "demoted": await _record(engine, demoted),
            "archived": await _record(engine, archived),
            "reembed": int(reembed.scalar_one()),
        }

    f = run_async(scenario)
    assert f["demoted"].demoted is False and f["demoted"].decay_score == 1.0
    assert f["archived"].status == "active" and f["archived"].decay_score == 1.0
    assert f["reembed"] >= 1  # archived record's chunks re-enqueued for embedding


# --- demoted excluded from default retrieval, included in deep ---------------


def test_demoted_excluded_by_default_included_in_deep(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        from tests.memory_fakes import HashingEmbeddings
        from tests.memory_harness import Pipeline, quiesce_outbox

        await quiesce_outbox(engine)
        pipeline = Pipeline(engine, embeddings=HashingEmbeddings())
        markers = await pipeline.seed(
            [{"title": "demote doc", "body_md": "DEMOTEMARK special retrieval fact"}]
        )
        rid = markers["demote doc"]
        # Demote it directly.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE brain.memory_records SET demoted = true WHERE id = :id"), {"id": rid}
            )
        shallow = await pipeline.packet(query="DEMOTEMARK special retrieval fact", scope="org")
        deep = await pipeline.packet(
            query="DEMOTEMARK special retrieval fact", scope="org", deep_search=True
        )
        return {"shallow": shallow, "deep": deep, "rid": str(rid)}

    f = run_async(scenario)
    shallow_ids = {str(c.record_id) for c in f["shallow"].citations}
    deep_ids = {str(c.record_id) for c in f["deep"].citations}
    assert f["rid"] not in shallow_ids  # demoted → hidden by default
    assert f["rid"] in deep_ids  # deep_search reveals it


# --- §12 reflection → episodic record → next packet --------------------------


def test_task_reflection_persists_and_is_retrievable(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        from tests.memory_fakes import HashingEmbeddings
        from tests.memory_harness import Pipeline, quiesce_outbox

        await quiesce_outbox(engine)
        pipeline = Pipeline(engine, embeddings=HashingEmbeddings())
        pid = await _make_project(engine)
        tid = await _make_task(engine, pid)
        gateway = _CannedGateway(
            json.dumps(
                {
                    "goal": "ship REFLECTMARK widget",
                    "what_worked": "TDD",
                    "what_surprised": "flaky fixtures",
                    "do_differently": "function-scoped fixtures",
                    "org_should_remember": "avoid module-scoped DB fixtures",
                }
            )
        )
        result = await memory.generate_task_reflection(
            engine,
            gateway,
            project_id=pid,
            task_id=tid,
            agent_id="eng_backend",
            context="implemented the REFLECTMARK widget",
            model_alias=None,
        )
        # Ingest so the reflection is retrievable, then fetch a packet.
        await _ingest(pipeline)
        packet = await pipeline.packet(
            query="REFLECTMARK widget reflection", scope=f"project:{pid}"
        )
        has_reflection = await memory.task_has_reflection(engine, tid)
        return {"result": result, "packet": packet, "has_reflection": has_reflection}

    f = run_async(scenario)
    assert f["result"].rubric.goal.startswith("ship REFLECTMARK")
    assert f["has_reflection"] is True  # the done-block hook now passes
    cited = {str(c.record_id) for c in f["packet"].citations}
    assert str(f["result"].record_id) in cited  # retrieved in the next packet


async def _ingest(pipeline: Any) -> None:
    from titan.memory.ingest import EMBEDDERS_GROUP, STREAM_MEMORY_INGEST, process_ingest_batch

    await pipeline.qdrant.ensure_collections()
    await pipeline.bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
    for _ in range(40):
        published = await pipeline.relay.relay_once()
        handled = await process_ingest_batch(pipeline.embedder, pipeline.bus, "cur", block_ms=1)
        if not published and not handled:
            break


def test_reflection_gate_blocks_without_reflection(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> tuple[bool, bool]:
        tid = uuid.uuid4()
        before = await memory.task_has_reflection(engine, tid)
        # An episodic record with the subtype but MISSING rubric fields is not valid.
        await _seed_record(
            engine, metadata={"record_subtype": "reflection", "goal": "g"}, title="partial"
        )
        return before, await memory.task_has_reflection(engine, tid)

    before, after = run_async(scenario)
    assert before is False and after is False  # no schema-valid reflection for the task


# --- §11.2 lessons -----------------------------------------------------------


def test_file_lesson_creates_protected_record_and_node(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        scope = f"project:{await _make_project(engine)}"
        result = await memory.file_lesson(
            engine,
            memory.LessonIn(
                scope=scope,
                title="Shared fixtures cause flakiness",
                context="sprint 3 QA",
                observation="3 intermittent failures",
                root_cause="module-scoped DB fixture",
                recommendation="use function scope",
                evidence=["task:abc", "pr:41"],
                importance=0.8,
            ),
            source_agent="agent.memory_agent",
            actor_agent="agent.memory_agent",
        )
        rec = await _record(engine, result.record_id)
        async with engine.connect() as conn:
            node = await repo.get_kg_node_id(
                conn, scope=scope, node_type="lesson", name="Shared fixtures cause flakiness"
            )
            derived = await conn.execute(
                text(
                    "SELECT count(*) FROM brain.kg_edges e JOIN brain.kg_nodes s ON s.id = e.src "
                    "WHERE e.edge_type = 'DERIVED_FROM' AND s.id = :nid"
                ),
                {"nid": node},
            )
        return {"rec": rec, "node": node, "derived": int(derived.scalar_one())}

    f = run_async(scenario)
    assert f["rec"].metadata["record_subtype"] == "lesson"
    assert f["node"] is not None
    assert f["derived"] >= 1  # DERIVED_FROM edge to its evidence


# --- §11.1 pattern miner -----------------------------------------------------


def test_pattern_miner_creates_novel_patterns(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        pid = await _make_project(engine)
        # Seed two decision records for the project.
        for i in range(2):
            await _seed_record(
                engine,
                scope=f"project:{pid}",
                project_id=pid,
                kind="episodic",
                metadata={"message_type": "DecisionLog"},
                title=f"decision {i}",
                age_days=1,
                importance=0.6,
            )
        server = FakeQdrantServer()
        qdrant = QdrantIndex(server.client())
        await qdrant.ensure_collections()
        gateway = _CannedGateway(
            json.dumps([{"name": "Redis allow-list", "description": "JWT refresh via Redis"}])
        )
        report = await patterns.mine_patterns(
            engine, gateway, qdrant, StubEmbeddings(), project_id=pid, model_alias=None
        )
        async with engine.connect() as conn:
            pattern = await conn.execute(
                text(
                    "SELECT count(*) FROM brain.memory_records "
                    "WHERE scope = :s AND metadata ->> 'record_subtype' = 'pattern'"
                ),
                {"s": f"project:{pid}"},
            )
        return {"report": report, "pattern_count": int(pattern.scalar_one())}

    f = run_async(scenario)
    assert f["report"].decisions_scanned == 2
    assert f["report"].novel == 1
    assert f["pattern_count"] == 1


# --- §11.3 promotion (multi-signal) ------------------------------------------


def test_promotion_requires_signal(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        server = FakeQdrantServer()
        qdrant = QdrantIndex(server.client())
        await qdrant.ensure_collections()
        # A single-project lesson with no signal → rejected.
        lone = await _seed_record(
            engine,
            scope=f"project:{uuid.uuid4()}",
            metadata={"record_subtype": "lesson"},
            title="Lonely lesson",
        )
        rejected = None
        try:
            await patterns.promote_record(
                engine,
                qdrant,
                StubEmbeddings(),
                patterns.PromotionRequest(record_id=lone),
                actor_agent="memory_agent",
            )
        except Exception as exc:
            rejected = exc
        # The SAME lesson title in two different projects → two-project signal.
        title = f"Recurring lesson {uuid.uuid4().hex[:6]}"
        await _seed_record(
            engine,
            scope=f"project:{uuid.uuid4()}",
            metadata={"record_subtype": "lesson"},
            title=title,
        )
        second = await _seed_record(
            engine,
            scope=f"project:{uuid.uuid4()}",
            metadata={"record_subtype": "lesson"},
            title=title,
        )
        promoted = await patterns.promote_record(
            engine,
            qdrant,
            StubEmbeddings(),
            patterns.PromotionRequest(record_id=second),
            actor_agent="memory_agent",
        )
        org = await _record(engine, promoted.org_record_id)
        return {"rejected": rejected, "promoted": promoted, "org": org}

    f = run_async(scenario)
    assert getattr(f["rejected"], "status", None) == 409  # no signal
    assert f["promoted"].signal == "two-projects"
    assert f["org"].scope == "org"  # cloned into org scope with provenance


# --- TITAN-BRN-024: agents cannot write org scope directly -------------------


def test_direct_org_write_from_agent_is_rejected(api: dict[str, Any]) -> None:
    """The promotion path never lets an agent write org directly — the same guard
    write_events enforces (§11.3 / TITAN-BRN-024)."""
    engine = api["engine"]

    async def scenario() -> str:
        batch = memory.MemoryEventBatch(
            source_agent="agent.eng_backend",
            events=[
                memory.MemoryEvent(
                    kind="semantic", scope="org", title="sneaky", body_md="org write"
                )
            ],
        )
        try:
            async with engine.begin() as conn:
                await memory.write_events(
                    conn, batch, source_agent="agent.eng_backend", actor_agent="agent.eng_backend"
                )
        except Exception as exc:
            return str(exc)
        return "ALLOWED"

    detail = run_async(scenario)
    assert "TITAN-BRN-024" in detail and "403" in detail


# --- endpoints (happy + 403 + validation) ------------------------------------


def _swap_infra(api: dict[str, Any]) -> tuple[Any, Any, Any]:
    app = api["client"].app
    server = FakeQdrantServer()
    run_async(QdrantIndex(server.client()).ensure_collections)
    app.state.http = server.client()
    app.state.llm_gateway = _CannedGateway(
        json.dumps(
            {
                "goal": "g",
                "what_worked": "w",
                "what_surprised": "s",
                "do_differently": "d",
                "org_should_remember": "r",
            }
        )
    )
    return app, server, None


def test_curation_endpoint(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    body = {"dry_run": True}
    assert (
        client.post(
            "/api/v1/memory/admin/curation/run", json=body, headers=token_for("operator")
        ).status_code
        == 403
    )  # operator lacks memory:admin
    ok = client.post("/api/v1/memory/admin/curation/run", json=body, headers=token_for("admin"))
    assert ok.status_code == 200, ok.text
    assert ok.json()["dry_run"] is True
    assert client.post("/api/v1/memory/admin/curation/run", json=body).status_code == 401


def test_reflection_and_status_endpoints(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    app = api["client"].app
    original = app.state.llm_gateway
    _swap_infra(api)
    try:
        pid = _api_project(api)
        tid = _task_via_engine(api, pid)
        # Before: no reflection.
        status0 = client.get(
            f"/api/v1/memory/tasks/{tid}/reflection-status", headers=token_for("viewer")
        )
        assert status0.status_code == 200 and status0.json()["has_reflection"] is False
        body = {"project_id": pid, "task_id": tid, "agent_id": "eng_backend", "context": "did work"}
        # viewer lacks memory:write → 403.
        assert (
            client.post(
                "/api/v1/memory/reflections/task", json=body, headers=token_for("viewer")
            ).status_code
            == 403
        )
        created = client.post(
            "/api/v1/memory/reflections/task", json=body, headers=token_for("admin")
        )
        assert created.status_code == 201, created.text
        # After: the done-block hook now passes.
        status1 = client.get(
            f"/api/v1/memory/tasks/{tid}/reflection-status", headers=token_for("viewer")
        )
        assert status1.json()["has_reflection"] is True
        # validation: missing context → 422.
        assert (
            client.post(
                "/api/v1/memory/reflections/task",
                json={"project_id": pid, "task_id": tid, "agent_id": "a"},
                headers=token_for("admin"),
            ).status_code
            == 422
        )
        assert client.post("/api/v1/memory/reflections/task", json=body).status_code == 401
    finally:
        app.state.llm_gateway = original


def test_lesson_endpoint(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    app = api["client"].app
    original = app.state.llm_gateway
    _swap_infra(api)
    try:
        pid = _api_project(api)
        body = {
            "scope": f"project:{pid}",
            "title": "Endpoint lesson",
            "context": "c",
            "observation": "o",
            "root_cause": "rc",
            "recommendation": "rec",
        }
        assert (
            client.post(
                "/api/v1/memory/lessons", json=body, headers=token_for("viewer")
            ).status_code
            == 403
        )
        ok = client.post("/api/v1/memory/lessons", json=body, headers=token_for("admin"))
        assert ok.status_code == 201, ok.text
        # org scope is rejected (lessons are project-scoped).
        assert (
            client.post(
                "/api/v1/memory/lessons", json={**body, "scope": "org"}, headers=token_for("admin")
            ).status_code
            == 422
        )
    finally:
        app.state.llm_gateway = original


def test_patterns_and_promote_endpoints(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    app = api["client"].app
    original = app.state.llm_gateway
    app.state.llm_gateway = _CannedGateway("[]")  # no candidates → empty miner run
    server = FakeQdrantServer()
    run_async(QdrantIndex(server.client()).ensure_collections)
    app.state.http = server.client()
    try:
        pid = _api_project(api)
        # operator lacks memory:admin.
        assert (
            client.post(
                "/api/v1/memory/admin/patterns/mine",
                json={"project_id": pid},
                headers=token_for("operator"),
            ).status_code
            == 403
        )
        ok = client.post(
            "/api/v1/memory/admin/patterns/mine",
            json={"project_id": pid},
            headers=token_for("admin"),
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["novel"] == 0
        # promote unknown record → 404.
        missing = client.post(
            "/api/v1/memory/admin/promote",
            json={"record_id": str(uuid.uuid4())},
            headers=token_for("admin"),
        )
        assert missing.status_code == 404
    finally:
        app.state.llm_gateway = original


def test_sprint_retro_endpoint(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    pid = _api_project(api)
    body = {"project_id": pid, "task_ids": []}
    assert (
        client.post(
            "/api/v1/memory/reflections/sprint", json=body, headers=token_for("viewer")
        ).status_code
        == 403
    )
    ok = client.post("/api/v1/memory/reflections/sprint", json=body, headers=token_for("admin"))
    assert ok.status_code == 201, ok.text
    assert ok.json()["reflections_aggregated"] == 0


# --- adversarial-review regression tests -------------------------------------


def test_delete_noops_when_a_later_upsert_is_pending(api: dict[str, Any]) -> None:
    """Review fix 1: archive enqueues a delete, then restore enqueues a later
    upsert for the same chunk; processing the delete must NOT remove the point
    (the later upsert owns the final state), or the restored record goes
    invisible (TITAN-BRN-020)."""
    engine = api["engine"]

    async def scenario() -> bool:
        from tests.memory_fakes import HashingEmbeddings
        from tests.memory_harness import Pipeline, quiesce_outbox
        from titan.memory.ingest import EMBEDDERS_GROUP, STREAM_MEMORY_INGEST

        await quiesce_outbox(engine)
        pipeline = Pipeline(engine, embeddings=HashingEmbeddings())
        markers = await pipeline.seed([{"title": "raced", "body_md": "RACEMARK fact"}])
        rid = markers["raced"]
        async with engine.connect() as conn:
            chunk_id = (await repo.list_chunks(conn, rid))[0].id
        assert str(chunk_id) in await pipeline.qdrant.list_point_ids("titan_memory")
        # Archive's delete first, then restore's upsert (higher outbox id).
        async with engine.begin() as conn:
            await repo.insert_outbox(conn, chunk_id=chunk_id, op="delete")
            await repo.insert_outbox(conn, chunk_id=chunk_id, op="upsert")
        await pipeline.relay.relay_once()
        # Process ONLY the delete (published first) — it must no-op.
        deliveries = await pipeline.bus.consume(
            STREAM_MEMORY_INGEST, EMBEDDERS_GROUP, "race", count=1, block_ms=1
        )
        await pipeline.embedder.handle(deliveries[0])
        return str(chunk_id) in await pipeline.qdrant.list_point_ids("titan_memory")

    assert run_async(scenario) is True  # the point survived the superseded delete


def test_sprint_retro_does_not_aggregate_cross_project_reflections(api: dict[str, Any]) -> None:
    """Review fix 2: a task id from ANOTHER project cannot pull that project's
    reflection into this sprint's summary (TITAN-BRN-003)."""
    engine = api["engine"]

    async def scenario() -> int:
        pid_a = await _make_project(engine)
        pid_b = await _make_project(engine)
        tid_b = await _make_task(engine, pid_b)
        # A reflection belonging to project B, linked to task B.
        await _seed_record(
            engine,
            scope=f"project:{pid_b}",
            project_id=pid_b,
            metadata={"record_subtype": "reflection", "org_should_remember": "SECRET-B"},
            title="B reflection",
        )
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE brain.memory_records SET source_task_id = :t "
                    "WHERE title = 'B reflection'"
                ),
                {"t": tid_b},
            )
        # Sprint retro for project A that (maliciously) passes B's task id.
        result = await memory.sprint_retrospective(
            engine, project_id=pid_a, task_ids=[tid_b], actor_agent="pm"
        )
        return result.reflections_aggregated

    assert run_async(scenario) == 0  # B's reflection is not leaked into A's sprint


def test_miner_run_is_ledger_visible_even_with_zero_novel(api: dict[str, Any]) -> None:
    """Review fix 3: a miner run with no decisions still appends a ledger row
    (TITAN-BRN-023 'miner runs are visible in the ledger')."""
    engine = api["engine"]

    async def scenario() -> int:
        pid = await _make_project(engine)
        server = FakeQdrantServer()
        qdrant = QdrantIndex(server.client())
        await qdrant.ensure_collections()
        # No decision records → empty run, but must still be logged.
        await patterns.mine_patterns(
            engine, _CannedGateway("[]"), qdrant, StubEmbeddings(), project_id=pid, model_alias=None
        )
        async with engine.connect() as conn:
            n = await conn.execute(
                text(
                    "SELECT count(*) FROM brain.memory_ledger "
                    "WHERE detail ->> 'miner_run' = 'true' AND detail ->> 'scope' = :s"
                ),
                {"s": f"project:{pid}"},
            )
        return int(n.scalar_one())

    assert run_async(scenario) == 1  # the empty run left an audit trail


def test_reflection_endpoint_unknown_project_is_422_not_500(api: dict[str, Any]) -> None:
    """Review fix 4: a well-formed but nonexistent project/task is a clean 422,
    not a 500 FK crash."""
    client, token_for = api["client"], api["token_for"]
    app = client.app
    original = app.state.llm_gateway
    app.state.llm_gateway = _CannedGateway(
        json.dumps(
            {
                "goal": "g",
                "what_worked": "w",
                "what_surprised": "s",
                "do_differently": "d",
                "org_should_remember": "r",
            }
        )
    )
    try:
        body = {
            "project_id": str(uuid.uuid4()),  # not a real project
            "task_id": str(uuid.uuid4()),
            "agent_id": "a",
            "context": "x",
        }
        r = client.post("/api/v1/memory/reflections/task", json=body, headers=token_for("admin"))
        assert r.status_code == 422, r.text
    finally:
        app.state.llm_gateway = original


def test_restore_reconstitutes_a_compacted_body(api: dict[str, Any]) -> None:
    """Review fix 5: accessing a compacted record restores its full body from the
    cold store (not the truncated stub)."""
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        rid = await _seed_record(
            engine, kind="episodic", status="archived", age_days=200, title="tocompact"
        )
        await memory.run_curation(engine, actor_agent="memory_agent", dry_run=False)
        stub = (await _record(engine, rid)).body_md
        async with engine.begin() as conn:
            await memory.get_record_detail(conn, rid, actor_agent="viewer", task_id=None)
        restored = await _record(engine, rid)
        async with engine.connect() as conn:
            cold_left = (
                await conn.execute(
                    text("SELECT count(*) FROM brain.memory_bodies_cold WHERE record_id = :r"),
                    {"r": rid},
                )
            ).scalar_one()
        return {"stub": stub, "restored": restored, "cold_left": int(cold_left)}

    f = run_async(scenario)
    assert f["stub"].startswith("[compacted]")  # was compacted
    assert "distinctive content" in f["restored"].body_md  # full body reconstituted
    assert f["restored"].status == "active"
    assert f["cold_left"] == 0  # cold entry consumed on restore


# keep an explicit reference so the imports read as intentional
_ = (curation.DESTRUCTIVE_APPROVAL_LIMIT, patterns.NOVELTY_THRESHOLD)
