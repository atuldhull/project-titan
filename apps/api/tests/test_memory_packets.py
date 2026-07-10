"""§6 end-to-end: hybrid retrieval → RRF → rerank → Memory Packet, against the
real schema (PG FTS is real; Qdrant/embeddings/reranker are the deterministic
fakes). Covers the Phase-36 DoD: zero cross-project leakage over 100 varied
queries (TITAN-BRN-003), the lexical leg surfacing exact identifiers, the §6.2
packet contract (citations on every chunk, section order, budgets), the Redis
packet cache (TITAN-BRN-012), and the ~30-pair smoke eval in BOTH reranker
modes."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from tests.memory_fakes import FakeQdrantServer, StubEmbeddings
from tests.memory_harness import (
    Pipeline,
)
from tests.memory_harness import (
    create_project as _project,
)
from tests.memory_harness import (
    overlap_scorer as _overlap_scorer,
)
from tests.memory_harness import (
    quiesce_outbox as _quiesce,
)
from titan.memory.packets import MemoryPacketOut, PacketRequest, packet_cache_key
from titan.memory.qdrant import QdrantIndex
from titan.memory.retrieval import CrossEncoderReranker, LlmListwiseReranker, NoopReranker

pytestmark = pytest.mark.schema

EVAL_FIXTURE = Path(__file__).parent / "golden" / "memory_eval_smoke.json"


# --- DoD: zero cross-project leakage over 100 varied queries -------------------


def test_no_cross_project_leakage_over_100_queries(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    project_a, project_b = _project(api, "alpha"), _project(api, "beta")

    async def scenario() -> list[str]:
        await _quiesce(engine)
        pipeline = Pipeline(engine)
        await pipeline.seed(
            [
                {
                    "title": f"ALPHA doc {i}",
                    "body_md": f"ALPHAMARKER{i:03d} alpha-only detail number {i} about "
                    "deploys, retries, budgets, reviews and incidents.",
                }
                for i in range(10)
            ],
            scope=f"project:{project_a}",
        )
        await pipeline.seed(
            [
                {
                    "title": f"BETA doc {i}",
                    "body_md": f"BETAMARKER{i:03d} beta-only detail number {i} about "
                    "deploys, retries, budgets, reviews and incidents.",
                }
                for i in range(10)
            ],
            scope=f"project:{project_b}",
        )
        await pipeline.seed(
            [{"title": "Org note", "body_md": "ORGMARKER shared convention for everyone."}]
        )
        leaks: list[str] = []
        subjects = ["deploys", "retries", "budgets", "reviews", "incidents"]
        for i in range(100):
            # Vary the queries — including DIRECT probes for the other project's
            # markers, which must still never leak (TITAN-BRN-003).
            query = [
                f"what do we know about {subjects[i % 5]} number {i % 10}",
                f"BETAMARKER{i % 10:03d} details",
                f"beta-only detail {i % 10}",
                f"alpha and beta {subjects[i % 5]}",
            ][i % 4]
            packet = await pipeline.packet(query=query, scope=f"project:{project_a}")
            for section in ("procedural", "semantic", "episodic"):
                for item in getattr(packet.sections, section):
                    if "BETAMARKER" in item or "beta-only" in item:
                        leaks.append(f"q{i}: {query!r} -> {item[:60]!r}")
        return leaks

    leaks = run_async(scenario)
    assert leaks == []  # zero cross-project leakage (TITAN-BRN-003)


# --- DoD: the lexical leg catches exact identifiers -----------------------------


def test_lexical_leg_surfaces_exact_identifier_missed_by_dense(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]

    async def scenario() -> tuple[bool, list[str]]:
        await _quiesce(engine)
        pipeline = Pipeline(engine)
        # 60 distractors so dense top-40 (random-hash vectors) misses the target.
        distractors = [
            {"title": f"noise {i}", "body_md": f"unrelated filler document number {i}"}
            for i in range(60)
        ]
        target = {
            "title": "connection reset investigation",
            "body_md": "Root cause: ERR_CONN_RESET_9931 fires when the pooler drops "
            "idle sockets; raise tcp keepalive.",
        }
        ids = await pipeline.seed([*distractors, target])
        packet = await pipeline.packet(query="ERR_CONN_RESET_9931", scope="org")
        target_id = ids["connection reset investigation"]
        hit = any(c.record_id == target_id for c in packet.citations)
        return hit, [str(c.record_id) for c in packet.citations]

    hit, _citations = run_async(scenario)
    assert hit  # found via websearch_to_tsquery even though dense missed it


def test_lexical_leg_finds_identifier_wrapped_in_natural_language(api: dict[str, Any]) -> None:
    """'what do we know about ERR_X' must still hit ERR_X's record: the
    identifier gets its own lexical probe (websearch_to_tsquery would AND the
    prose words into the match otherwise)."""
    engine: AsyncEngine = api["engine"]

    async def scenario() -> bool:
        await _quiesce(engine)
        pipeline = Pipeline(engine)
        distractors = [
            {"title": f"nl noise {i}", "body_md": f"assorted org text number {i}"}
            for i in range(60)
        ]
        target = {
            "title": "keepalive fix",
            "body_md": "ERR_KEEPALIVE_7331 root cause: the pooler dropped idle sockets.",
        }
        ids = await pipeline.seed([*distractors, target])
        packet = await pipeline.packet(
            query="what do we know about ERR_KEEPALIVE_7331", scope="org"
        )
        return any(c.record_id == ids["keepalive fix"] for c in packet.citations)

    assert run_async(scenario)


# --- DoD: the §6.2 packet contract -----------------------------------------------


def test_packet_shape_citations_and_section_order(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    project = _project(api, "packet")

    async def scenario() -> tuple[MemoryPacketOut, dict[str, Any]]:
        await _quiesce(engine)
        pipeline = Pipeline(engine)
        await pipeline.seed(
            [
                {"title": "checklist", "kind": "procedural", "body_md": "PKT deploy checklist"},
                {"title": "component", "kind": "semantic", "body_md": "PKT auth owns /auth"},
                {"title": "event", "kind": "episodic", "body_md": "PKT QA rejected PR 41"},
            ],
            scope=f"project:{project}",
        )
        packet = await pipeline.packet(
            query="PKT deploy auth QA",
            scope=f"project:{project}",
            assembled_for={"agent": "backend_agent", "call_seq": 3},
        )
        # The §6.2 JSON round-trips through its own schema.
        as_json = json.loads(packet.model_dump_json())
        MemoryPacketOut.model_validate(as_json)
        return packet, as_json

    packet, as_json = run_async(scenario)
    # Every included chunk carries a citation (§6.2 rule a).
    included = (
        len(packet.sections.procedural)
        + len(packet.sections.semantic)
        + len(packet.sections.episodic)
    )
    assert included >= 3
    assert len(packet.citations) == included
    assert all(c.score is not None for c in packet.citations)
    # Sections keyed by record kind; identity present and non-empty (rule c).
    assert any("checklist" in item for item in packet.sections.procedural)
    assert any("component" in item for item in packet.sections.semantic)
    assert any("event" in item for item in packet.sections.episodic)
    assert packet.sections.identity_context.startswith("Project packet project")
    assert packet.sections.working_summary == ""  # the slot exists, filled later
    assert packet.token_cost <= packet.budget_tokens
    assert as_json["assembled_for"]["agent"] == "backend_agent"
    # §6.2 key set, exactly.
    assert set(as_json) == {
        "packet_id",
        "assembled_for",
        "sections",
        "citations",
        "token_cost",
        "budget_tokens",
    }


def test_budget_fill_stops_at_85_percent_and_identity_is_non_evictable(
    api: dict[str, Any],
) -> None:
    engine: AsyncEngine = api["engine"]

    async def scenario() -> tuple[MemoryPacketOut, MemoryPacketOut]:
        await _quiesce(engine)
        pipeline = Pipeline(engine)
        big_body = " ".join(f"budgetword{i}" for i in range(300))
        await pipeline.seed(
            [{"title": f"big {i}", "body_md": f"BUDGETMARK {big_body}"} for i in range(6)]
        )
        roomy = await pipeline.packet(query="BUDGETMARK budgetword1", scope="org")
        tight = await pipeline.packet(
            query="BUDGETMARK budgetword1", scope="org", budget_tokens=256
        )
        return roomy, tight

    roomy, tight = run_async(scenario)
    assert roomy.token_cost <= int(roomy.budget_tokens * 0.85)
    assert len(roomy.citations) >= 1
    # A 256-token budget cannot fit any ~330-token chunk — but identity survives.
    assert tight.citations == []
    assert tight.sections.identity_context != ""
    assert tight.token_cost <= int(tight.budget_tokens * 0.85)


# --- TITAN-BRN-012: the packet cache ----------------------------------------------


def test_second_identical_call_serves_from_cache(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]

    async def scenario() -> tuple[MemoryPacketOut, MemoryPacketOut, int, MemoryPacketOut]:
        await _quiesce(engine)
        pipeline = Pipeline(engine)
        await pipeline.seed([{"title": "cached", "body_md": "CACHEMARK cached content"}])
        first = await pipeline.packet(query="CACHEMARK", scope="org")
        embed_calls_after_first = pipeline.embeddings.calls
        second = await pipeline.packet(
            query="CACHEMARK", scope="org", assembled_for={"agent": "qa_agent"}
        )
        assert pipeline.embeddings.calls == embed_calls_after_first  # no re-retrieval
        ttl = int(
            await pipeline.redis.ttl(
                packet_cache_key(PacketRequest(query="CACHEMARK", scope="org"))
            )
        )
        different = await pipeline.packet(query="CACHEMARK other", scope="org")
        return first, second, ttl, different

    first, second, ttl, different = run_async(scenario)
    assert second.packet_id == first.packet_id  # the SAME cached packet
    assert second.assembled_for is not None
    assert second.assembled_for.agent == "qa_agent"  # attribution re-attached
    assert 0 < ttl <= 300  # §4.5 TTL
    assert different.packet_id != first.packet_id  # different query → miss


def test_cache_key_isolates_scopes(api: dict[str, Any]) -> None:
    a = packet_cache_key(PacketRequest(query="q", scope="org"))
    b = packet_cache_key(PacketRequest(query="q", scope=f"project:{uuid.uuid4()}"))
    assert a != b
    assert a.startswith("rc:org:")


def test_cache_key_is_case_sensitive_like_retrieval(api: dict[str, Any]) -> None:
    """Retrieval is case-sensitive (the ALL-CAPS identifier probes fire only on
    'OAUTH', never 'oauth'), so case-variant queries must never share a cache
    entry — a lowered key would serve one query the other's weaker packet."""
    upper = packet_cache_key(PacketRequest(query="tell me about OAUTH", scope="org"))
    lower = packet_cache_key(PacketRequest(query="tell me about oauth", scope="org"))
    assert upper != lower
    # Whitespace variants ARE the same retrieval (shape_queries normalises them).
    spaced = packet_cache_key(PacketRequest(query="tell  me   about OAUTH ", scope="org"))
    assert spaced == upper


def test_broken_reranker_degrades_to_fusion_order(api: dict[str, Any]) -> None:
    """A reranker crash (model load failure, gateway outage) must never fail
    packet assembly — retrieval degrades to the fused order."""
    engine: AsyncEngine = api["engine"]

    class ExplodingReranker:
        async def scores(self, query: str, texts: list[str]) -> list[float]:
            raise RuntimeError("model not available (test)")

    async def scenario() -> MemoryPacketOut:
        await _quiesce(engine)
        pipeline = Pipeline(engine)
        pipeline.reranker = ExplodingReranker()
        await pipeline.seed([{"title": "resilient", "body_md": "DEGRADEMARK survives"}])
        return await pipeline.packet(query="DEGRADEMARK", scope="org")

    packet = run_async(scenario)
    assert len(packet.citations) >= 1  # assembled despite the reranker being down


def test_redis_outage_degrades_to_uncached_assembly(api: dict[str, Any]) -> None:
    """The cache is an accelerator, not a dependency (TITAN-BRN-012 is SHOULD):
    a Redis outage assembles packets fresh instead of failing the request."""
    engine: AsyncEngine = api["engine"]

    class BrokenRedis:
        async def get(self, key: str) -> None:
            raise ConnectionError("redis down (test)")

        async def set(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("redis down (test)")

    async def scenario() -> MemoryPacketOut:
        await _quiesce(engine)
        pipeline = Pipeline(engine)
        pipeline.redis = BrokenRedis()
        await pipeline.seed([{"title": "nocache", "body_md": "NOCACHEMARK content"}])
        return await pipeline.packet(query="NOCACHEMARK", scope="org")

    packet = run_async(scenario)
    assert len(packet.citations) >= 1  # served fresh, no 500


def test_build_reranker_mode_switch(api: dict[str, Any]) -> None:
    """on→cross-encoder only with the FULL onnx stack installed (else degrade to
    LLM); llm→listwise; off→the zero-latency no-op."""
    from titan.memory.packets import build_reranker
    from titan.settings import Settings

    def settings_for(mode: Any) -> Settings:
        return Settings(reranker=mode, _env_file=None)

    gateway = object()
    assert isinstance(build_reranker(settings_for("off"), gateway), NoopReranker)
    assert isinstance(build_reranker(settings_for("llm"), gateway), LlmListwiseReranker)
    # This dev environment does NOT ship the onnx stack → "on" degrades to LLM
    # instead of 500ing at first request (the packaged-extra path is live-only).
    on_reranker = build_reranker(settings_for("on"), gateway)
    from titan.memory.packets import _cross_encoder_deps_present

    if _cross_encoder_deps_present():
        assert isinstance(on_reranker, CrossEncoderReranker)
    else:
        assert isinstance(on_reranker, LlmListwiseReranker)


def test_noop_reranker_emits_descending_positional_scores(api: dict[str, Any]) -> None:
    reranker = NoopReranker()
    scores = run_async(lambda: reranker.scores("q", ["first", "second", "third"]))
    assert scores[0] > scores[1] > scores[2]  # fused RRF order handed to stage 5


def test_noop_mode_still_lets_freshness_importance_modulate(api: dict[str, Any]) -> None:
    """§6.1 stage 5 applies in EVERY mode: with the no-op reranker, a fresh and
    important record can overtake a fused near-neighbour — that is the stage's
    purpose, not a rank-preservation bug."""
    import uuid as _uuid

    from titan.memory.retrieval import RankedChunk, final_order

    def chunk(importance: float, decay: float) -> RankedChunk:
        return RankedChunk(
            chunk_id=_uuid.uuid4(),
            record_id=_uuid.uuid4(),
            kind="semantic",
            text="x",
            token_count=1,
            importance=importance,
            decay_score=decay,
        )

    stale = chunk(importance=0.0, decay=0.0)  # fused rank 2 of 4
    fresh = chunk(importance=1.0, decay=1.0)  # fused rank 3 of 4
    chunks = [chunk(0.5, 1.0), stale, fresh, chunk(0.5, 0.5)]
    noop_scores = [4.0, 3.0, 2.0, 1.0]  # what NoopReranker emits
    ordered = final_order(chunks, noop_scores)
    assert ordered.index(fresh) < ordered.index(stale)  # modulation happened


def test_corrupt_cache_entry_is_treated_as_a_miss(api: dict[str, Any]) -> None:
    """A garbage or schema-skewed cached value must never 500 for the TTL —
    it is evicted and the packet is assembled fresh."""
    engine: AsyncEngine = api["engine"]

    async def scenario() -> tuple[MemoryPacketOut, Any]:
        await _quiesce(engine)
        pipeline = Pipeline(engine)
        await pipeline.seed([{"title": "poisoncache", "body_md": "POISONCACHE body"}])
        request = PacketRequest(query="POISONCACHE", scope="org")
        key = packet_cache_key(request)
        await pipeline.redis.set(key, "{not valid json][", ex=300)
        packet = await pipeline.packet(query="POISONCACHE", scope="org")
        return packet, await pipeline.redis.get(key)

    packet, stored = run_async(scenario)
    assert len(packet.citations) >= 1  # assembled fresh, no 500
    assert stored is None or "packet_id" in str(stored)  # bad entry evicted/replaced


# --- both reranker modes pass the smoke eval (DoD) ---------------------------------


@pytest.mark.parametrize("mode", ["on", "llm", "off"])
def test_smoke_eval_passes_in_both_reranker_modes(api: dict[str, Any], mode: str) -> None:
    engine: AsyncEngine = api["engine"]
    fixture = json.loads(EVAL_FIXTURE.read_text(encoding="utf-8"))

    async def scenario() -> tuple[int, int, list[str]]:
        await _quiesce(engine)
        pipeline = Pipeline(engine, mode=mode)
        ids = await pipeline.seed(
            [
                {
                    "marker": r["marker"],
                    "title": r["title"],
                    "kind": r["kind"],
                    "body_md": r["body_md"],
                }
                for r in fixture["records"]
            ]
        )
        hits = 0
        misses: list[str] = []
        for pair in fixture["pairs"]:
            packet = await pipeline.packet(query=pair["query"], scope="org")
            expected = ids[pair["expected_marker"]]
            top10 = [c.record_id for c in packet.citations[:10]]
            if expected in top10:
                hits += 1
            else:
                misses.append(pair["query"])
        return hits, len(fixture["pairs"]), misses

    hits, total, misses = run_async(scenario)
    recall = hits / total
    assert recall >= 0.9, f"mode={mode} recall@10={recall:.2f}; misses: {misses}"


# --- kb leg + endpoint ---------------------------------------------------------------


def test_procedural_kinds_also_search_titan_kb(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]

    async def scenario() -> tuple[list[str], list[str]]:
        await _quiesce(engine)
        pipeline = Pipeline(engine)
        await pipeline.seed(
            [{"title": "rb", "kind": "procedural", "body_md": "KBMARK runbook step"}]
        )
        pipeline.server.search_calls.clear()
        await pipeline.packet(query="KBMARK", scope="org", kinds=["procedural"])
        with_kb = list(pipeline.server.search_calls)
        pipeline.server.search_calls.clear()
        await pipeline.packet(query="KBMARK semantic", scope="org", kinds=["semantic"])
        without_kb = list(pipeline.server.search_calls)
        return with_kb, without_kb

    with_kb, without_kb = run_async(scenario)
    assert "titan_kb" in with_kb  # §6.1: procedural kinds fan out to the KB
    assert "titan_kb" not in without_kb


def test_packets_endpoint_rbac_and_validation(api: dict[str, Any]) -> None:
    server = FakeQdrantServer()
    app = api["client"].app
    original = (app.state.http, app.state.llm_gateway, app.state.memory_reranker)
    app.state.http = server.client()
    app.state.llm_gateway = StubEmbeddings()
    app.state.memory_reranker = CrossEncoderReranker(score_fn=_overlap_scorer)
    try:
        run_async(lambda: QdrantIndex(server.client()).ensure_collections())
        anonymous = api["client"].post(
            "/api/v1/memory/packets", json={"query": "q", "scope": "org"}
        )
        assert anonymous.status_code == 401
        bad_scope = api["client"].post(
            "/api/v1/memory/packets",
            json={"query": "q", "scope": "everywhere"},
            headers=api["token_for"]("viewer"),
        )
        assert bad_scope.status_code == 422
        bad_kind = api["client"].post(
            "/api/v1/memory/packets",
            json={"query": "q", "scope": "org", "kinds": ["rumor"]},
            headers=api["token_for"]("viewer"),
        )
        assert bad_kind.status_code == 422
        ok = api["client"].post(
            "/api/v1/memory/packets",
            json={"query": "anything at all", "scope": "org"},
            headers=api["token_for"]("viewer"),  # *:read covers memory:read
        )
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["budget_tokens"] == 4000 and "packet_id" in body
    finally:
        app.state.http, app.state.llm_gateway, app.state.memory_reranker = original
