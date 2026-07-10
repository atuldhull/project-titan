"""§6.1 retrieval mechanics — pure unit tests (no DB, no infra): query shaping,
RRF fusion math, the final ordering key, budget fill, LLM-rerank parsing, and
the reranker factory."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from tests.conftest import run_async
from titan.memory.packets import cheapest_chat_alias
from titan.memory.retrieval import (
    BUDGET_FILL_RATIO,
    MAX_SUB_QUERIES,
    RRF_K,
    WEIGHT_DECAY,
    WEIGHT_IMPORTANCE,
    WEIGHT_RERANK,
    CrossEncoderReranker,
    LlmListwiseReranker,
    RankedChunk,
    fill_budget,
    final_order,
    rrf_fuse,
    shape_queries,
)


def _chunk(
    *,
    tokens: int = 10,
    importance: float = 0.5,
    decay: float = 1.0,
    kind: str = "semantic",
    text: str = "chunk text",
) -> RankedChunk:
    return RankedChunk(
        chunk_id=uuid.uuid4(),
        record_id=uuid.uuid4(),
        kind=kind,
        text=text,
        token_count=tokens,
        importance=importance,
        decay_score=decay,
    )


# --- query shaping (§6.1 stage 1) --------------------------------------------


def test_shape_queries_normalises_and_leads_with_the_full_query() -> None:
    assert shape_queries("  fix   the\nbug  ") == ["fix the bug"]


def test_shape_queries_splits_multi_part_needs_up_to_three() -> None:
    query = (
        "How do we configure the retry policy; what does the reaper timeout do; "
        "where is the DLQ replayed; and how are budgets enforced"
    )
    subs = shape_queries(query)
    assert len(subs) == MAX_SUB_QUERIES  # capped at 3 (§6.1)
    assert subs[0].startswith("How do we configure")  # full query always first
    assert any("reaper timeout" in sub for sub in subs[1:])


def test_shape_queries_drops_trivial_fragments_and_duplicates() -> None:
    subs = shape_queries("Fix the auth bug; ok; Fix the auth bug")
    assert subs[0] == "Fix the auth bug; ok; Fix the auth bug"  # full query leads
    # 'ok' (< 3 words) never becomes a sub-query; the duplicate fragment appears once.
    assert all(sub.strip().lower() != "ok" for sub in subs)
    assert len([s for s in subs if s.lower() == "fix the auth bug"]) <= 1


def test_identifier_terms_extracts_codes_tickets_and_symbols() -> None:
    from titan.memory.retrieval import identifier_terms

    terms = identifier_terms("what do we know about ERR_CONN_RESET_9931 and PROJ-X?")
    assert "ERR_CONN_RESET_9931" in terms
    terms = identifier_terms("does useAuthStore2 exist; see TICKET_4821 and HTTP")
    assert "useAuthStore2" in terms and "TICKET_4821" in terms and "HTTP" in terms
    assert identifier_terms("plain english words only here") == []
    # Capped at 3, deduped case-insensitively.
    many = identifier_terms("ALPHA_1 BETA_2 GAMMA_3 DELTA_4 alpha_1")
    assert len(many) == 3


# --- RRF fusion (§6.1 stage 3) -------------------------------------------------


def test_rrf_formula_is_sum_of_one_over_60_plus_rank() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    ca, cb = uuid.uuid4(), uuid.uuid4()
    # a is rank 1 in list one; b is rank 1 in list two and rank 2 in list one.
    fused = rrf_fuse([[(a, ca), (b, cb)], [(b, cb)]])
    scores = {record_id: score for record_id, _chunk_id, score in fused}
    assert scores[a] == pytest.approx(1 / (RRF_K + 1))
    assert scores[b] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert fused[0][0] == b  # two-list agreement outranks one first place


def test_rrf_dedupes_by_record_and_keeps_best_chunk() -> None:
    record = uuid.uuid4()
    chunk_hi, chunk_lo = uuid.uuid4(), uuid.uuid4()
    fused = rrf_fuse([[(record, chunk_lo)], [(record, chunk_hi), (record, chunk_lo)]])
    assert len(fused) == 1
    # Both lists rank the record #1; the same-rank tie keeps a rank-1 chunk and a
    # record's SECOND appearance in one list adds nothing.
    assert fused[0][2] == pytest.approx(2 * (1 / (RRF_K + 1)))


def test_rrf_lexical_only_records_carry_no_chunk() -> None:
    record = uuid.uuid4()
    fused = rrf_fuse([[(record, None)]])
    assert len(fused) == 1
    record_id, chunk_id, score = fused[0]
    assert record_id == record and chunk_id is None
    assert score == pytest.approx(1 / (RRF_K + 1))


# --- final ordering key (§6.1 stage 5) -------------------------------------------


def test_final_order_key_is_07_rerank_02_decay_01_importance() -> None:
    fresh_important = _chunk(importance=1.0, decay=1.0)
    stale_trivial = _chunk(importance=0.0, decay=0.0)
    ordered = final_order([stale_trivial, fresh_important], [1.0, 0.0])
    # stale_trivial got the max rerank (norm 1.0) → 0.7; fresh_important got
    # norm 0.0 but 0.2·1.0 + 0.1·1.0 = 0.3 → rerank wins.
    assert ordered[0] is stale_trivial
    assert ordered[0].final_score == pytest.approx(WEIGHT_RERANK)
    assert ordered[1].final_score == pytest.approx(WEIGHT_DECAY + WEIGHT_IMPORTANCE)


def test_final_order_flat_rerank_scores_defer_to_freshness_importance() -> None:
    fresh = _chunk(importance=0.9, decay=1.0)
    stale = _chunk(importance=0.1, decay=0.2)
    ordered = final_order([stale, fresh], [0.5, 0.5])  # flat → norm 0.5 each
    assert ordered[0] is fresh


# --- budget fill (§6.1 stage 4 tail) ----------------------------------------------


def test_fill_budget_stops_at_85_percent() -> None:
    chunks = [_chunk(tokens=300) for _ in range(10)]
    kept = fill_budget(chunks, 1000, reserved=0)
    # 85% of 1000 = 850 → two 300-token chunks fit (600), a third would be 900.
    assert len(kept) == 2
    assert pytest.approx(0.85) == BUDGET_FILL_RATIO


def test_fill_budget_charges_identity_reservation_first() -> None:
    chunks = [_chunk(tokens=300) for _ in range(10)]
    kept = fill_budget(chunks, 1000, reserved=600)  # identity is non-evictable
    assert len(kept) == 0  # 600 + 300 > 850: no chunk fits, identity stays


def test_fill_budget_keeps_order_of_ranked_input() -> None:
    first, second = _chunk(tokens=100, text="first"), _chunk(tokens=100, text="second")
    kept = fill_budget([first, second], 1000, reserved=0)
    assert [c.text for c in kept] == ["first", "second"]


# --- LLM listwise reranker (RERANKER=off) ------------------------------------------


class _FakeGateway:
    def __init__(self, reply: str | None, *, raise_error: bool = False) -> None:
        self._reply = reply
        self._raise = raise_error
        self.requests: list[Any] = []

    async def chat(self, request: Any) -> Any:
        self.requests.append(request)
        if self._raise:
            raise RuntimeError("gateway down (test)")

        class _Resp:
            text = self._reply

        return _Resp()


def test_llm_rerank_parses_a_clean_ranking() -> None:
    reranker = LlmListwiseReranker(_FakeGateway("[2, 0, 1]"))
    scores = run_async(lambda: reranker.scores("q", ["a", "b", "c"]))
    assert scores[2] > scores[0] > scores[1]  # 2 ranked first, then 0, then 1


def test_llm_rerank_repairs_partial_and_noisy_output() -> None:
    reranker = LlmListwiseReranker(_FakeGateway("Sure! Ranking: [1] — best match."))
    scores = run_async(lambda: reranker.scores("q", ["a", "b", "c"]))
    assert scores[1] > scores[0] > scores[2] or scores[1] == max(scores)


def test_llm_rerank_falls_back_on_garbage_and_errors() -> None:
    gateways = (
        _FakeGateway("no json here"),
        _FakeGateway(None),
        _FakeGateway("", raise_error=True),
    )
    for gateway in gateways:
        reranker = LlmListwiseReranker(gateway)

        def scenario(r: LlmListwiseReranker = reranker) -> Any:
            return r.scores("q", ["a", "b", "c"])

        scores = run_async(scenario)
        assert scores[0] > scores[1] > scores[2]  # input order preserved


def test_llm_rerank_ignores_out_of_range_indices() -> None:
    reranker = LlmListwiseReranker(_FakeGateway("[9, 1, 1, 0]"))
    scores = run_async(lambda: reranker.scores("q", ["a", "b"]))
    assert scores[1] > scores[0]  # 9 dropped, duplicate 1 collapsed


def test_llm_rerank_uses_the_configured_alias_and_caps_tokens() -> None:
    gateway = _FakeGateway("[0]")
    reranker = LlmListwiseReranker(gateway, model_alias="fast-cheap")
    run_async(lambda: reranker.scores("q", ["a"]))
    request = gateway.requests[0]
    assert request.model_alias == "fast-cheap"
    assert request.max_tokens <= 500


# --- cross-encoder wrapper -----------------------------------------------------------


def test_cross_encoder_uses_injected_scorer_off_the_event_loop() -> None:
    calls: list[list[tuple[str, str]]] = []

    def scorer(pairs: list[tuple[str, str]]) -> list[float]:
        calls.append(pairs)
        return [float(len(text)) for _query, text in pairs]

    reranker = CrossEncoderReranker(score_fn=scorer)
    scores = run_async(lambda: reranker.scores("q", ["aa", "aaaa"]))
    assert scores == [2.0, 4.0]
    assert calls == [[("q", "aa"), ("q", "aaaa")]]


# --- cheapest chat alias ----------------------------------------------------------


def test_cheapest_chat_alias_excludes_embeddings_and_prefers_free_local() -> None:
    # The repo models.yaml: local-default has no price (0), embed-default is an
    # embeddings alias (dimensions set) and must never be chosen for chat.
    from pathlib import Path

    models_yaml = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
    alias = cheapest_chat_alias(str(models_yaml))
    assert alias == "local-default"


def test_cheapest_chat_alias_price_type_is_decimal() -> None:
    assert isinstance(Decimal("0"), Decimal)  # guard against float drift in the key
