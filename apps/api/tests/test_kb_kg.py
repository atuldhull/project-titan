"""Phase 39 — Knowledge Base + knowledge graph (Vol 5 §7/§10/§14,
TITAN-BRN-013/-014/-021/-022).

Covers the DoD:
- a DIRECT KB write attempt returns 403 while the proposal → approve path creates
  a new current version with full provenance (proposer, approver, change note) —
  both tested; a new version supersedes the previous one out of titan_kb;
- routing by type: coding_standard/decision_record need a human sign-off (an
  agent-only approval is 403); other types approve on Code-Reviewer authority;
- a packet requested with pins:["…"] always contains that doc's chunks, verified
  even when the query is unrelated;
- a seeded fixture graph (project → components → decisions → lessons) returns
  correctly from a depth-3 traversal and depth-4 paths are excluded; edge-type
  filter + scope isolation hold;
- the §7.3 structured writers create the right nodes/edges (TITAN-BRN-013) and
  dedup exact names / active edges (TITAN-BRN-014's deterministic half);
- KB chunks embed into titan_kb (not titan_memory) through the reused §5.1
  pipeline, and rebuild reconciles titan_kb from PostgreSQL;
- the GraphKgSink fills the §13 step-11 hook with real writes.

Tests per new endpoint: happy path + 403 + validation."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from tests.memory_fakes import FakeQdrantServer, StubEmbeddings, deterministic_vector
from titan.memory import kb, kg
from titan.memory import repository as repo
from titan.memory import service as memory
from titan.memory.ingest import process_ingest_batch
from titan.memory.qdrant import QdrantIndex

pytestmark = pytest.mark.schema


# --- helpers -----------------------------------------------------------------


def _proposal_body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "doc_type": "best_practice",
        "slug": f"bp-{uuid.uuid4().hex[:10]}",
        "title": "Idempotent consumers on Redis Streams",
        "body_md": "# Idempotency\n\nUse a stable event id and a seen-ledger for replays.",
        "change_note": "initial version",
        "rationale": "recurring bug across services",
        "tags": ["redis", "streams"],
    }
    base.update(over)
    return base


class _KbPipeline:
    """Full KB write + ingest path on real PG + fake Qdrant/embeddings/redis, so
    a proposal is approved and its chunks embedded into titan_kb hermetically."""

    def __init__(self, engine: AsyncEngine) -> None:
        import fakeredis.aioredis

        from titan.events.service import RedisStreamBus
        from titan.memory.ingest import EMBEDDERS_GROUP, STREAM_MEMORY_INGEST, MemoryEmbedder
        from titan.memory.retrieval import NoopReranker
        from titan.memory.service import IngestRelay

        self.engine = engine
        self.server = FakeQdrantServer()
        self.qdrant = QdrantIndex(self.server.client())
        self.embeddings = StubEmbeddings()
        self.redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.bus = RedisStreamBus(self.redis)
        self.relay = IngestRelay(self.engine, self.bus)
        self.embedder = MemoryEmbedder(self.engine, self.bus, self.qdrant, self.embeddings)
        self.reranker = NoopReranker()
        self._group = (STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)

    async def bootstrap(self) -> None:
        await self.qdrant.ensure_collections()
        await self.bus.ensure_group(*self._group)

    async def propose(self, **over: Any) -> Any:
        proposal = kb.KbProposalIn(**_proposal_body(**over))
        return await kb.create_proposal(
            self.engine, self.qdrant, self.embeddings, proposal, proposed_by="agent.code_reviewer"
        )

    async def approve(self, proposal_id: uuid.UUID, *, is_human: bool = True) -> Any:
        return await kb.approve_proposal(
            self.engine, proposal_id, approver="alice@titan.local", approver_is_human=is_human
        )

    async def drain(self) -> None:
        for _ in range(40):
            published = await self.relay.relay_once()
            handled = await process_ingest_batch(self.embedder, self.bus, "kb", block_ms=1)
            if not published and not handled:
                break

    async def packet(self, **kwargs: Any) -> Any:
        request = memory.PacketRequest(**kwargs)
        return await memory.assemble_packet(
            self.engine, self.redis, self.qdrant, self.embeddings, self.reranker, request
        )


def _section_text(packet: Any) -> str:
    """All of a MemoryPacketOut's section text concatenated (the DTO has no
    prompt renderer — that lives on the runtime packet)."""
    s = packet.sections
    parts = [s.identity_context, *s.procedural, *s.semantic, *s.episodic, s.working_summary]
    return " ".join(parts)


# --- KB: direct write is forbidden (TITAN-BRN-021) ---------------------------


def test_direct_kb_write_is_forbidden(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    # Even an owner (the '*' wildcard) cannot mutate the KB directly.
    post = client.post("/api/v1/kb/documents", json={"anything": True}, headers=token_for("owner"))
    assert post.status_code == 403, post.text
    put = client.put(
        "/api/v1/kb/documents/some-slug", json={"anything": True}, headers=token_for("owner")
    )
    assert put.status_code == 403
    # Unauthenticated is a 401, not a 403 (the guard runs first).
    assert client.post("/api/v1/kb/documents", json={}).status_code == 401


# --- KB: proposal → approval creates a version with full provenance -----------


def test_propose_then_approve_materialises_current_version(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        pipeline = _KbPipeline(engine)
        await pipeline.bootstrap()
        slug = f"py-std-{uuid.uuid4().hex[:8]}"
        proposal = await pipeline.propose(
            doc_type="best_practice", slug=slug, title="Python Service Standard"
        )
        result = await pipeline.approve(proposal.id, is_human=True)
        await pipeline.drain()
        async with engine.connect() as conn:
            doc = await repo.get_kb_document_by_slug(conn, slug)
            versions = await repo.list_kb_versions(conn, doc.id)
            rec = (
                await conn.execute(
                    text(
                        "SELECT kind, scope, status, metadata ->> 'kb_slug' AS slug "
                        "FROM brain.memory_records WHERE id = :id"
                    ),
                    {"id": result.record_id},
                )
            ).one()
            outbox = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.ingest_outbox o "
                        "JOIN brain.memory_chunks c ON c.id = o.chunk_id "
                        "WHERE c.record_id = :rid AND o.collection = 'titan_kb'"
                    ),
                    {"rid": result.record_id},
                )
            ).scalar_one()
            ledger = (
                await conn.execute(
                    text(
                        "SELECT actor_agent FROM brain.memory_ledger "
                        "WHERE :rid = ANY(record_ids) AND action = 'write'"
                    ),
                    {"rid": result.record_id},
                )
            ).one()
        kb_points = await pipeline.qdrant.count("titan_kb")
        return {
            "doc": doc,
            "versions": versions,
            "rec": rec,
            "outbox": int(outbox),
            "ledger_actor": ledger.actor_agent,
            "result": result,
            "kb_points": kb_points,
        }

    facts = run_async(scenario)
    assert facts["doc"].current_version == 1 and facts["doc"].status == "active"
    v = facts["versions"][0]
    # Full provenance chain (TITAN-BRN-021): proposer + approver + change note.
    assert v.proposed_by == "agent.code_reviewer"
    assert v.approved_by == "alice@titan.local"
    assert v.change_note == "initial version"
    # Materialised as a procedural, org-scope, active KB record.
    assert facts["rec"].kind == "procedural" and facts["rec"].scope == "org"
    assert facts["rec"].status == "active"
    assert facts["rec"].slug == facts["doc"].slug
    assert facts["outbox"] >= 1  # chunks queued to titan_kb, not titan_memory
    assert facts["ledger_actor"] == "alice@titan.local"
    assert facts["result"].chunk_count >= 1
    assert facts["kb_points"] >= 1  # embedded into titan_kb by the reused pipeline


def test_new_version_supersedes_previous_out_of_titan_kb(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        pipeline = _KbPipeline(engine)
        await pipeline.bootstrap()
        slug = f"std-{uuid.uuid4().hex[:8]}"
        p1 = await pipeline.propose(doc_type="best_practice", slug=slug, title="Std v1")
        r1 = await pipeline.approve(p1.id)
        await pipeline.drain()
        points_after_v1 = await pipeline.qdrant.count("titan_kb")
        p2 = await pipeline.propose(
            doc_type="best_practice", slug=slug, title="Std v2", change_note="tightened"
        )
        r2 = await pipeline.approve(p2.id)
        await pipeline.drain()
        async with engine.connect() as conn:
            doc = await repo.get_kb_document_by_slug(conn, slug)
            v1status = (
                await conn.execute(
                    text("SELECT status, superseded_by FROM brain.memory_records WHERE id = :id"),
                    {"id": r1.record_id},
                )
            ).one()
            v2status = (
                await conn.execute(
                    text("SELECT status FROM brain.memory_records WHERE id = :id"),
                    {"id": r2.record_id},
                )
            ).scalar_one()
            versions = await repo.list_kb_versions(conn, doc.id)
        return {
            "doc": doc,
            "v1status": v1status,
            "v2status": v2status,
            "versions": versions,
            "r2": r2,
            "points_after_v1": points_after_v1,
            "points_after_v2": await pipeline.qdrant.count("titan_kb"),
        }

    facts = run_async(scenario)
    assert facts["doc"].current_version == 2
    assert facts["r2"].version == 2
    assert facts["v1status"].status == "superseded"
    assert str(facts["v1status"].superseded_by) == str(facts["r2"].record_id)
    assert facts["v2status"] == "active"
    assert len(facts["versions"]) == 2  # both versions remain queryable (§10.2)
    # Only the current version is in titan_kb — v1's points were superseded out,
    # v2's embedded in, so the count did not grow by v1+v2.
    assert facts["points_after_v2"] == facts["points_after_v1"]


def test_protected_type_needs_human_signoff(api: dict[str, Any]) -> None:
    """coding_standard/decision_record require a human approver; a Code-Reviewer
    agent's approval alone is 403. Non-protected types approve on agent
    authority (§10.3 routing)."""
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        pipeline = _KbPipeline(engine)
        await pipeline.bootstrap()
        # A coding_standard: agent approval is rejected, human approval succeeds.
        cs = await pipeline.propose(doc_type="coding_standard", slug=f"cs-{uuid.uuid4().hex[:8]}")
        assert cs.requires_human is True
        agent_denied = None
        try:
            await pipeline.approve(cs.id, is_human=False)
        except Exception as exc:  # ProblemException
            agent_denied = exc
        human_ok = await pipeline.approve(cs.id, is_human=True)
        # A best_practice: an agent (Code Reviewer) CAN approve it alone.
        bp = await pipeline.propose(doc_type="best_practice", slug=f"bp-{uuid.uuid4().hex[:8]}")
        assert bp.requires_human is False
        agent_ok = await pipeline.approve(bp.id, is_human=False)
        return {"denied": agent_denied, "human_ok": human_ok, "agent_ok": agent_ok}

    facts = run_async(scenario)
    assert facts["denied"] is not None
    assert getattr(facts["denied"], "status", None) == 403
    assert facts["human_ok"].version == 1
    assert facts["agent_ok"].version == 1


def test_approve_unknown_is_404_and_double_approve_is_409(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        pipeline = _KbPipeline(engine)
        await pipeline.bootstrap()
        missing = None
        try:
            await pipeline.approve(uuid.uuid4())
        except Exception as exc:
            missing = exc
        p = await pipeline.propose(slug=f"once-{uuid.uuid4().hex[:8]}")
        await pipeline.approve(p.id)
        second = None
        try:
            await pipeline.approve(p.id)
        except Exception as exc:
            second = exc
        return {"missing": missing, "second": second}

    facts = run_async(scenario)
    assert getattr(facts["missing"], "status", None) == 404
    assert getattr(facts["second"], "status", None) == 409  # already approved


def test_slug_type_is_immutable(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> Any:
        pipeline = _KbPipeline(engine)
        await pipeline.bootstrap()
        slug = f"dual-{uuid.uuid4().hex[:8]}"
        p = await pipeline.propose(doc_type="best_practice", slug=slug)
        await pipeline.approve(p.id)
        # A second proposal reusing the slug with a DIFFERENT doc_type is rejected.
        try:
            await pipeline.propose(doc_type="coding_standard", slug=slug)
        except Exception as exc:
            return exc
        return None

    err = run_async(scenario)
    assert getattr(err, "status", None) == 422


# --- KB: validation (DTO) ----------------------------------------------------


def test_proposal_validation() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        kb.KbProposalIn(**_proposal_body(doc_type="rumor"))  # bad doc_type
    with pytest.raises(ValidationError):
        kb.KbProposalIn(**_proposal_body(slug="Not A Slug"))  # bad slug shape
    with pytest.raises(ValidationError):
        kb.KbProposalIn(**_proposal_body(body_md=""))  # empty body
    with pytest.raises(ValidationError):
        body = _proposal_body()
        body.pop("change_note")
        kb.KbProposalIn(**body)  # missing change_note


# --- KB: HTTP surface (happy + 403 + validation + auth) ----------------------


def test_kb_endpoints_http(api: dict[str, Any]) -> None:
    client, token_for = api["client"], api["token_for"]
    app = client.app
    server = FakeQdrantServer()
    run_async(QdrantIndex(server.client()).ensure_collections)
    original_http, original_gateway = app.state.http, app.state.llm_gateway
    app.state.http = server.client()
    app.state.llm_gateway = StubEmbeddings()
    try:
        slug = f"http-bp-{uuid.uuid4().hex[:8]}"
        body = _proposal_body(doc_type="best_practice", slug=slug)
        # viewer lacks kb:propose → 403.
        assert (
            client.post("/api/v1/kb/proposals", json=body, headers=token_for("viewer")).status_code
            == 403
        )
        # unauthenticated → 401.
        assert client.post("/api/v1/kb/proposals", json=body).status_code == 401
        # invalid doc_type → 422.
        bad = client.post(
            "/api/v1/kb/proposals",
            json=_proposal_body(doc_type="nope", slug=slug),
            headers=token_for("maintainer"),
        )
        assert bad.status_code == 422
        # maintainer (human, kb:propose) proposes → 201.
        created = client.post("/api/v1/kb/proposals", json=body, headers=token_for("maintainer"))
        assert created.status_code == 201, created.text
        proposal_id = created.json()["id"]
        assert created.json()["requires_human"] is False
        # viewer lacks kb:approve → 403.
        assert (
            client.post(
                f"/api/v1/kb/proposals/{proposal_id}/approve", headers=token_for("viewer")
            ).status_code
            == 403
        )
        # maintainer (human) approves → 200.
        approved = client.post(
            f"/api/v1/kb/proposals/{proposal_id}/approve", headers=token_for("maintainer")
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["version"] == 1
        # the document + its history are readable (§10.2).
        got = client.get(f"/api/v1/kb/documents/{slug}", headers=token_for("viewer"))
        assert got.status_code == 200
        assert got.json()["document"]["current_version"] == 1
        assert len(got.json()["versions"]) == 1
        # unknown slug → 404.
        assert (
            client.get("/api/v1/kb/documents/no-such", headers=token_for("viewer")).status_code
            == 404
        )
    finally:
        app.state.http, app.state.llm_gateway = original_http, original_gateway


def test_protected_type_http_human_only(api: dict[str, Any]) -> None:
    """A coding_standard approved by a human maintainer succeeds at the HTTP
    surface (the human sign-off route)."""
    client, token_for = api["client"], api["token_for"]
    app = client.app
    server = FakeQdrantServer()
    run_async(QdrantIndex(server.client()).ensure_collections)
    original_http, original_gateway = app.state.http, app.state.llm_gateway
    app.state.http = server.client()
    app.state.llm_gateway = StubEmbeddings()
    try:
        slug = f"http-cs-{uuid.uuid4().hex[:8]}"
        created = client.post(
            "/api/v1/kb/proposals",
            json=_proposal_body(doc_type="coding_standard", slug=slug),
            headers=token_for("maintainer"),
        )
        assert created.status_code == 201
        assert created.json()["requires_human"] is True
        approved = client.post(
            f"/api/v1/kb/proposals/{created.json()['id']}/approve",
            headers=token_for("admin"),
        )
        assert approved.status_code == 200, approved.text
    finally:
        app.state.http, app.state.llm_gateway = original_http, original_gateway


# --- pin-by-slug (TITAN-BRN-022) ---------------------------------------------


def test_pin_forces_kb_doc_into_packet_even_when_query_unrelated(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        pipeline = _KbPipeline(engine)
        await pipeline.bootstrap()
        slug = f"pinme-{uuid.uuid4().hex[:8]}"
        proposal = await pipeline.propose(
            doc_type="coding_standard",
            slug=slug,
            title="PINMARK coding standard",
            body_md="# PINMARK\n\nAll services MUST use PINMARK idempotency keys.",
        )
        result = await pipeline.approve(proposal.id, is_human=True)
        await pipeline.drain()
        # A query with NOTHING to do with the pinned doc — the pin forces it in.
        packet = await pipeline.packet(
            query="quarterly capacity planning spreadsheet",
            scope="org",
            kinds=[],  # not even asking for procedural retrieval
            pins=[slug],
            budget_tokens=4000,
        )
        return {"packet": packet, "record_id": str(result.record_id)}

    facts = run_async(scenario)
    packet = facts["packet"]
    pinned = [c for c in packet.citations if c.pinned]
    # TITAN-BRN-022: the pinned doc is present, flagged as pinned (not ranked),
    # and its text is actually in the packet — even though the query is unrelated
    # AND procedural retrieval was not requested.
    assert pinned, "the pinned doc must be cited via the pin path"
    assert all(str(c.record_id) == facts["record_id"] for c in pinned)
    assert "PINMARK" in _section_text(packet)
    # The pinned chunk is a pin, never double-counted as a ranked citation.
    ranked_for_doc = [
        c for c in packet.citations if str(c.record_id) == facts["record_id"] and not c.pinned
    ]
    assert not ranked_for_doc


# --- KG structured writers (§7.3, TITAN-BRN-013/-014) ------------------------


def test_structured_writers_create_nodes_and_edges(api: dict[str, Any]) -> None:
    engine = api["engine"]
    scope = f"project:{uuid.uuid4()}"

    async def scenario() -> dict[str, Any]:
        async with engine.begin() as conn:
            decision = await kg.record_decision(
                conn,
                scope=scope,
                name="ADR-001 use asyncpg",
                decided_for=[kg.KgNodeRef(node_type="component", name="api")],
                constrains=[kg.KgNodeRef(node_type="component", name="db-layer")],
            )
            component = await kg.record_component(
                conn, scope=scope, name="api", part_of=kg.KgNodeRef(node_type="project", name="P")
            )
            lesson = await kg.record_lesson(
                conn,
                scope=scope,
                name="prefer core over ORM",
                derived_from=kg.KgNodeRef(node_type="incident", name="INC-5"),
            )
        async with engine.connect() as conn:
            edges = (
                await conn.execute(
                    text(
                        "SELECT e.edge_type, s.node_type AS src_type, d.node_type AS dst_type "
                        "FROM brain.kg_edges e "
                        "JOIN brain.kg_nodes s ON s.id = e.src "
                        "JOIN brain.kg_nodes d ON d.id = e.dst "
                        "WHERE s.scope = :scope AND e.valid_to IS NULL"
                    ),
                    {"scope": scope},
                )
            ).all()
        return {"decision": decision, "component": component, "lesson": lesson, "edges": edges}

    facts = run_async(scenario)
    kinds = {(e.edge_type, e.src_type, e.dst_type) for e in facts["edges"]}
    assert ("DECIDED_FOR", "decision", "component") in kinds
    assert ("CONSTRAINS", "decision", "component") in kinds
    assert ("PART_OF", "component", "project") in kinds
    assert ("DERIVED_FROM", "lesson", "incident") in kinds


def test_ensure_node_dedups_and_merges(api: dict[str, Any]) -> None:
    """TITAN-BRN-014 deterministic half: the same (scope,type,name) imported
    twice yields ONE node with merged properties."""
    engine = api["engine"]
    scope = f"project:{uuid.uuid4()}"

    async def scenario() -> dict[str, Any]:
        async with engine.begin() as conn:
            first = await kg.ensure_node(
                conn, scope=scope, node_type="component", name="widget", properties={"lang": "py"}
            )
            second = await kg.ensure_node(
                conn, scope=scope, node_type="component", name="widget", properties={"team": "core"}
            )
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.kg_nodes WHERE scope = :s AND name = 'widget'"
                    ),
                    {"s": scope},
                )
            ).scalar_one()
            props = (
                await conn.execute(
                    text("SELECT properties FROM brain.kg_nodes WHERE id = :id"), {"id": first}
                )
            ).scalar_one()
        return {"first": first, "second": second, "count": int(row), "props": props}

    facts = run_async(scenario)
    assert facts["first"] == facts["second"]  # same node
    assert facts["count"] == 1
    props = facts["props"] if isinstance(facts["props"], dict) else {}
    assert props.get("lang") == "py" and props.get("team") == "core"  # merged


def test_add_edge_is_idempotent(api: dict[str, Any]) -> None:
    engine = api["engine"]
    scope = f"project:{uuid.uuid4()}"

    async def scenario() -> dict[str, Any]:
        async with engine.begin() as conn:
            a = await kg.ensure_node(conn, scope=scope, node_type="component", name="a")
            b = await kg.ensure_node(conn, scope=scope, node_type="project", name="b")
            first = await kg.add_edge(conn, src=a, dst=b, edge_type="PART_OF")
            second = await kg.add_edge(conn, src=a, dst=b, edge_type="PART_OF")
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.kg_edges "
                        "WHERE src = :a AND dst = :b AND valid_to IS NULL"
                    ),
                    {"a": a, "b": b},
                )
            ).scalar_one()
        return {"first": first, "second": second, "count": int(count)}

    facts = run_async(scenario)
    assert facts["first"] is not None and facts["second"] is None  # second no-op'd
    assert facts["count"] == 1


def test_supersede_decision_keeps_history(api: dict[str, Any]) -> None:
    engine = api["engine"]
    scope = f"project:{uuid.uuid4()}"

    async def scenario() -> dict[str, Any]:
        async with engine.begin() as conn:
            await kg.record_decision(
                conn,
                scope=scope,
                name="ADR-old",
                decided_for=[kg.KgNodeRef(node_type="component", name="svc")],
            )
            await kg.supersede_decision(
                conn, scope=scope, superseded_name="ADR-old", superseding_name="ADR-new"
            )
        async with engine.connect() as conn:
            old_id = await repo.get_kg_node_id(
                conn, scope=scope, node_type="decision", name="ADR-old"
            )
            expired = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.kg_edges "
                        "WHERE src = :old AND valid_to IS NOT NULL"
                    ),
                    {"old": old_id},
                )
            ).scalar_one()
            supersedes = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.kg_edges e "
                        "JOIN brain.kg_nodes s ON s.id = e.src "
                        "JOIN brain.kg_nodes d ON d.id = e.dst "
                        "WHERE e.edge_type = 'SUPERSEDES' AND s.name = 'ADR-new' "
                        "  AND d.name = 'ADR-old' AND s.scope = :scope"
                    ),
                    {"scope": scope},
                )
            ).scalar_one()
        return {"expired": int(expired), "supersedes": int(supersedes)}

    facts = run_async(scenario)
    assert facts["expired"] >= 1  # the old decision's edges are stamped, not deleted
    assert facts["supersedes"] == 1


# --- KG traversal depth cap (§7.1) -------------------------------------------


async def _seed_chain(engine: AsyncEngine, scope: str) -> dict[str, uuid.UUID]:
    """project ← component ← decision ← lesson ← (depth-4 node), oriented so an
    undirected walk from the project reaches them at depths 1..4."""
    async with engine.begin() as conn:
        project = await kg.ensure_node(conn, scope=scope, node_type="project", name="P")
        component = await kg.ensure_node(conn, scope=scope, node_type="component", name="C")
        decision = await kg.ensure_node(conn, scope=scope, node_type="decision", name="D")
        lesson = await kg.ensure_node(conn, scope=scope, node_type="lesson", name="L")
        deep = await kg.ensure_node(conn, scope=scope, node_type="incident", name="X4")
        await kg.add_edge(conn, src=component, dst=project, edge_type="PART_OF")
        await kg.add_edge(conn, src=decision, dst=component, edge_type="DECIDED_FOR")
        await kg.add_edge(conn, src=lesson, dst=decision, edge_type="DERIVED_FROM")
        await kg.add_edge(conn, src=deep, dst=lesson, edge_type="DERIVED_FROM")
    return {
        "project": project,
        "component": component,
        "decision": decision,
        "lesson": lesson,
        "deep": deep,
    }


def test_depth3_traversal_includes_depth3_excludes_depth4(api: dict[str, Any]) -> None:
    engine = api["engine"]
    scope = f"project:{uuid.uuid4()}"

    async def scenario() -> dict[str, Any]:
        ids = await _seed_chain(engine, scope)
        request = kg.KgQueryRequest(
            scope=scope, start=[kg.KgNodeRef(node_type="project", name="P")], max_depth=3
        )
        result = await memory.kg_query(engine, request)
        return {"ids": ids, "names": {n.name: n.depth for n in result.nodes}}

    facts = run_async(scenario)
    names = facts["names"]
    assert names.get("P") == 0
    assert names.get("C") == 1
    assert names.get("D") == 2
    assert names.get("L") == 3
    assert "X4" not in names  # depth-4 path excluded by the depth-3 cap


def test_traversal_edge_type_filter_and_scope_isolation(api: dict[str, Any]) -> None:
    engine = api["engine"]
    scope_a = f"project:{uuid.uuid4()}"
    scope_b = f"project:{uuid.uuid4()}"

    async def scenario() -> dict[str, Any]:
        await _seed_chain(engine, scope_a)
        # An isolated node in a DIFFERENT project sharing the name 'P'.
        async with engine.begin() as conn:
            await kg.ensure_node(conn, scope=scope_b, node_type="project", name="P")
            other = await kg.ensure_node(conn, scope=scope_b, node_type="component", name="secretC")
            p_b = await repo.get_kg_node_id(conn, scope=scope_b, node_type="project", name="P")
            await kg.add_edge(conn, src=other, dst=p_b, edge_type="PART_OF")
        # Filter to PART_OF only: from P we reach C (depth 1) but NOT D/L (those
        # are reached via DECIDED_FOR/DERIVED_FROM, now filtered out).
        filtered = await memory.kg_query(
            engine,
            kg.KgQueryRequest(
                scope=scope_a,
                start=[kg.KgNodeRef(node_type="project", name="P")],
                edge_types=["PART_OF"],
                max_depth=3,
            ),
        )
        # Scope isolation: querying scope_a must never surface scope_b's node.
        names_a = {n.name for n in filtered.nodes}
        return {"filtered": names_a}

    facts = run_async(scenario)
    assert facts["filtered"] == {"P", "C"}  # PART_OF-only: no D/L, no cross-project secretC


def test_kg_query_endpoint(api: dict[str, Any]) -> None:
    client, token_for, engine = api["client"], api["token_for"], api["engine"]
    scope = f"project:{uuid.uuid4()}"
    run_async(lambda: _seed_chain(engine, scope))
    body = {"scope": scope, "start": [{"node_type": "project", "name": "P"}], "max_depth": 3}
    ok = client.post("/api/v1/memory/kg/query", json=body, headers=token_for("viewer"))
    assert ok.status_code == 200, ok.text
    names = {n["name"] for n in ok.json()["nodes"]}
    assert {"P", "C", "D", "L"} <= names and "X4" not in names
    # validation: a bad edge_type is 422.
    bad = client.post(
        "/api/v1/memory/kg/query",
        json={"scope": scope, "edge_types": ["FROBNICATES"]},
        headers=token_for("viewer"),
    )
    assert bad.status_code == 422
    # depth > 3 is rejected by the DTO (hard cap).
    too_deep = client.post(
        "/api/v1/memory/kg/query",
        json={"scope": scope, "max_depth": 4},
        headers=token_for("viewer"),
    )
    assert too_deep.status_code == 422
    assert client.post("/api/v1/memory/kg/query", json=body).status_code == 401  # unauth


# --- collection-aware ingestion + rebuild ------------------------------------


def test_kb_chunks_land_in_titan_kb_not_titan_memory(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        pipeline = _KbPipeline(engine)
        await pipeline.bootstrap()
        p = await pipeline.propose(slug=f"coll-{uuid.uuid4().hex[:8]}", title="COLLMARK doc")
        result = await pipeline.approve(p.id)
        await pipeline.drain()
        # The KB chunk id must be a point in titan_kb and ABSENT from titan_memory.
        async with engine.connect() as conn:
            chunk_ids = [str(r.id) for r in await repo.list_chunks(conn, result.record_id)]
        kb_ids = await pipeline.qdrant.list_point_ids("titan_kb")
        mem_ids = await pipeline.qdrant.list_point_ids("titan_memory")
        return {"chunk_ids": chunk_ids, "kb_ids": kb_ids, "mem_ids": mem_ids}

    facts = run_async(scenario)
    assert facts["chunk_ids"], "KB record should have chunks"
    assert set(facts["chunk_ids"]) <= facts["kb_ids"]  # embedded into titan_kb
    assert not (set(facts["chunk_ids"]) & facts["mem_ids"])  # never into titan_memory


def test_rebuild_reconciles_titan_kb_from_postgres(api: dict[str, Any]) -> None:
    engine = api["engine"]

    async def scenario() -> dict[str, Any]:
        pipeline = _KbPipeline(engine)
        await pipeline.bootstrap()
        p = await pipeline.propose(slug=f"reb-{uuid.uuid4().hex[:8]}", title="REBUILDMARK doc")
        result = await pipeline.approve(p.id)
        await pipeline.drain()
        async with engine.connect() as conn:
            chunk_ids = {str(r.id) for r in await repo.list_chunks(conn, result.record_id)}
        # Seed a stray into titan_kb (no backing PG chunk) — rebuild must delete it.
        stray = str(uuid.uuid4())
        pipeline.server.seed_point("titan_kb", stray, deterministic_vector("stray"))
        report = await memory.rebuild_index(engine, pipeline.qdrant, pipeline.embeddings)
        kb_ids = await pipeline.qdrant.list_point_ids("titan_kb")
        mem_ids = await pipeline.qdrant.list_point_ids("titan_memory")
        return {
            "chunk_ids": chunk_ids,
            "kb_ids": kb_ids,
            "mem_ids": mem_ids,
            "stray": stray,
            "report": report,
        }

    facts = run_async(scenario)
    # KB chunks are reconciled INTO titan_kb; the stray is gone; nothing leaked
    # into titan_memory.
    assert facts["chunk_ids"] <= facts["kb_ids"]
    assert facts["stray"] not in facts["kb_ids"]
    assert not (facts["chunk_ids"] & facts["mem_ids"])
    assert "titan_kb" in facts["report"].collections


# --- GraphKgSink fills the §13 step-11 hook ----------------------------------


def test_graph_kg_sink_writes_real_nodes_and_edges(api: dict[str, Any]) -> None:
    engine = api["engine"]
    project_id = uuid.uuid4()
    task_id = uuid.uuid4()

    class _Event:
        def __init__(self, kind: str) -> None:
            self.kind = kind
            self.label = "DecisionLog"
            self.project_id = project_id
            self.task_id = task_id
            self.source_agent = "agent.eng_backend"

    async def scenario() -> dict[str, Any]:
        sink = kg.GraphKgSink(engine)
        await sink.emit([_Event("decision")])
        await sink.emit([_Event("decision")])  # idempotent second emit
        scope = f"project:{project_id}"
        async with engine.connect() as conn:
            nodes = (
                await conn.execute(
                    text("SELECT node_type, name FROM brain.kg_nodes WHERE scope = :s"),
                    {"s": scope},
                )
            ).all()
            edges = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.kg_edges e "
                        "JOIN brain.kg_nodes s ON s.id = e.src "
                        "WHERE s.scope = :s AND e.edge_type = 'DECIDED_FOR' AND e.valid_to IS NULL"
                    ),
                    {"s": scope},
                )
            ).scalar_one()
        return {"nodes": nodes, "edges": int(edges)}

    facts = run_async(scenario)
    types = {n.node_type for n in facts["nodes"]}
    assert "decision" in types and "project" in types
    assert facts["edges"] == 1  # idempotent — the second emit added no duplicate
