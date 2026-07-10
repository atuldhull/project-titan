"""The §6.1 retrieval pipeline (Vol 5; ADR-05-03, TITAN-BRN-003/010) — private
to `memory`.

    query ──shape (≤3 sub-queries)──▶ dense (Qdrant top-40/sub-query, scope+kind
    payload-filtered) ∥ lexical (PG FTS websearch_to_tsquery top-40)
    ──RRF Σ 1/(60+rank), dedupe by record_id, top-k──▶ hydrate from PostgreSQL
    (status='active', scope re-asserted) ──rerank──▶ order by
    0.7·rerank_norm + 0.2·decay_score + 0.1·importance ──▶ budget fill (85 %)

Scope isolation is enforced THREE times (TITAN-BRN-003): the Qdrant payload
filter, the lexical WHERE clause, and the hydration WHERE clause — a candidate
from another project cannot survive any leg. Reranking is pluggable
(:class:`Reranker`) with three first-class, tested modes: RERANKER=on (the
in-process cross-encoder), RERANKER=llm (the §6.1 listwise fallback through
the gateway), and RERANKER=off (a zero-latency pass-through — the §15 R4
low-RAM mode and the only one inside TITAN-BRN-010's ≤400 ms off budget).
"""

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.llm.service import ChatRequest, ChatResponse, EmbeddingsRequest, Message
from titan.memory import repository as repo
from titan.memory.ingest import EmbeddingsClient
from titan.memory.qdrant import QdrantIndex

logger = structlog.get_logger("titan.memory")

# §6.1 constants.
MAX_SUB_QUERIES = 3
DENSE_TOP_K = 40
LEXICAL_TOP_K = 40
FUSED_TOP_K = 24
RRF_K = 60
# §6.1 stage 5 — the final ordering key weights.
WEIGHT_RERANK = 0.7
WEIGHT_DECAY = 0.2
WEIGHT_IMPORTANCE = 0.1
# §6.1 stage 4 — fill until 85 % of budget_tokens.
BUDGET_FILL_RATIO = 0.85

_SENTENCE_SPLIT = re.compile(r"[;\n]+|\?\s+|(?<=[.!])\s+(?=[A-Z])")
_WS = re.compile(r"\s+")
# Identifier-shaped tokens — error codes, ticket ids, symbol names: a word
# carrying a digit or underscore, or ALL-CAPS of 4+. These get their own
# lexical probes so "what do we know about ERR_X" still hits ERR_X's record
# (websearch_to_tsquery ANDs every word; the surrounding prose would otherwise
# have to appear in the document too — defeating the leg's whole purpose).
_IDENTIFIER = re.compile(r"\b(?=\w*[0-9_])[A-Za-z_][A-Za-z0-9_]{3,}\b|\b[A-Z]{4,}\b")

# (query, chunk_text) pairs -> relevance scores; the cross-encoder's callable
# shape, injectable in tests.
ScoreFn = Callable[[list[tuple[str, str]]], list[float]]


class Reranker(Protocol):
    """Scores (query, text) pairs — higher is more relevant. Implemented by the
    in-process cross-encoder and the LLM listwise fallback (§6.1 stage 4)."""

    async def scores(self, query: str, texts: list[str]) -> list[float]: ...


@dataclass(frozen=True)
class RetrievalQuery:
    """One §6.1 request after validation: the caller's need plus the mandatory
    isolation scope (org + at most ONE project — TITAN-BRN-003)."""

    query: str
    scope: str  # 'org' | 'project:<uuid>'
    kinds: tuple[str, ...] = ()
    k: int = FUSED_TOP_K
    budget_tokens: int = 4000

    @property
    def scopes(self) -> tuple[str, ...]:
        """The ONLY scopes any leg may touch: org plus the caller's project."""
        return ("org",) if self.scope == "org" else ("org", self.scope)


@dataclass
class RankedChunk:
    """One retrieval candidate after hydration + ranking."""

    chunk_id: uuid.UUID
    record_id: uuid.UUID
    kind: str
    text: str
    token_count: int
    importance: float
    decay_score: float
    rerank_norm: float = 0.0
    final_score: float = 0.0


def shape_queries(query: str) -> list[str]:
    """§6.1 stage 1: normalise, and split a multi-part need into at most
    ``MAX_SUB_QUERIES`` sub-queries (the full query always leads). Deterministic
    — no LLM in the hot retrieval path."""
    normalised = _WS.sub(" ", query).strip()
    subs: list[str] = [normalised]
    for part in _SENTENCE_SPLIT.split(normalised):
        candidate = _WS.sub(" ", part or "").strip(" .!?")
        # A fragment is a useful sub-query only if it stands on its own.
        if candidate and candidate != normalised and len(candidate.split()) >= 3:
            subs.append(candidate)
    deduped: list[str] = []
    for sub in subs:
        if sub.lower() not in {d.lower() for d in deduped}:
            deduped.append(sub)
    return deduped[:MAX_SUB_QUERIES]


def identifier_terms(query: str, limit: int = 3) -> list[str]:
    """Identifier-shaped tokens in the query (error codes, ticket ids, symbol
    names) — each becomes its own lexical probe (ADR-05-03: the lexical leg
    exists precisely for these)."""
    terms: list[str] = []
    for match in _IDENTIFIER.finditer(query):
        token = match.group(0)
        if token.lower() not in {t.lower() for t in terms}:
            terms.append(token)
    return terms[:limit]


def rrf_fuse(
    ranked_lists: list[list[tuple[uuid.UUID, uuid.UUID | None]]],
) -> list[tuple[uuid.UUID, uuid.UUID | None, float]]:
    """§6.1 stage 3: Reciprocal Rank Fusion, ``score(d) = Σ 1/(60 + rank_i(d))``
    keyed on record_id (1-based ranks). Each list entry is (record_id, chunk_id
    | None); the fused result keeps each record's best-ranked chunk id. Ties
    break on record_id for determinism."""
    scores: dict[uuid.UUID, float] = {}
    best_chunk: dict[uuid.UUID, tuple[int, uuid.UUID | None]] = {}
    for ranked in ranked_lists:
        seen: set[uuid.UUID] = set()
        for position, (record_id, chunk_id) in enumerate(ranked, start=1):
            if record_id in seen:
                continue  # a record's LATER chunks in the same list add nothing
            seen.add(record_id)
            scores[record_id] = scores.get(record_id, 0.0) + 1.0 / (RRF_K + position)
            if chunk_id is not None:
                current = best_chunk.get(record_id)
                if current is None or position < current[0]:
                    best_chunk[record_id] = (position, chunk_id)
    fused = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
    return [
        (record_id, best_chunk.get(record_id, (0, None))[1], score) for record_id, score in fused
    ]


def final_order(chunks: list[RankedChunk], rerank_scores: list[float]) -> list[RankedChunk]:
    """§6.1 stage 5: ``0.7·rerank_norm + 0.2·decay_score + 0.1·importance``,
    with min-max-normalised rerank scores (a flat list normalises to 0.5 so the
    freshness/importance terms decide)."""
    if not chunks:
        return []
    low, high = min(rerank_scores), max(rerank_scores)
    span = high - low
    for chunk, raw in zip(chunks, rerank_scores, strict=True):
        chunk.rerank_norm = 0.5 if span == 0 else (raw - low) / span
        chunk.final_score = (
            WEIGHT_RERANK * chunk.rerank_norm
            + WEIGHT_DECAY * chunk.decay_score
            + WEIGHT_IMPORTANCE * chunk.importance
        )
    return sorted(chunks, key=lambda c: (-c.final_score, str(c.chunk_id)))


def fill_budget(
    chunks: list[RankedChunk], budget_tokens: int, *, reserved: int
) -> list[RankedChunk]:
    """§6.1 stage 4 tail: keep top-N until 85 % of ``budget_tokens`` is filled.
    ``reserved`` is the non-evictable identity_context cost, charged up front
    (§6.2 rule c)."""
    limit = int(budget_tokens * BUDGET_FILL_RATIO)
    kept: list[RankedChunk] = []
    used = reserved
    for chunk in chunks:
        if used + chunk.token_count > limit:
            break  # budget filled — lower-ranked chunks are evicted
        kept.append(chunk)
        used += chunk.token_count
    return kept


def _dense_filter(query: RetrievalQuery) -> dict[str, Any]:
    """The MANDATORY Qdrant payload filter (TITAN-BRN-003): org + the caller's
    own project only — never another project — plus the kind narrowing. Status
    is re-asserted at hydration (older points may predate the status field)."""
    must: list[dict[str, Any]] = [{"key": "scope", "match": {"any": list(query.scopes)}}]
    if query.kinds:
        must.append({"key": "kind", "match": {"any": list(query.kinds)}})
    return {"must": must}


async def retrieve(
    engine: AsyncEngine,
    qdrant: QdrantIndex,
    embeddings: EmbeddingsClient,
    reranker: Reranker,
    query: RetrievalQuery,
) -> list[RankedChunk]:
    """Stages 1–5 of §6.1; returns budget-unbounded ranked chunks (the packet
    assembler applies the 85 % fill after charging identity_context)."""
    sub_queries = shape_queries(query.query)
    if not sub_queries:
        return []

    # -- stage 2: hybrid candidate generation, dense ∥ lexical -----------------
    # The lexical leg probes the full query PLUS each identifier-shaped token on
    # its own: websearch_to_tsquery ANDs every word, so an identifier wrapped in
    # prose ("what do we know about ERR_X") would otherwise never match the
    # document that only contains ERR_X.
    lexical_queries = [sub_queries[0]]
    lexical_queries += [term for term in identifier_terms(query.query) if term != sub_queries[0]]
    dense_lists, *lexical_row_lists = await asyncio.gather(
        _dense_leg(qdrant, embeddings, query, sub_queries),
        *[_lexical_leg(engine, query, lex_query) for lex_query in lexical_queries],
    )
    lexical_lists: list[list[tuple[uuid.UUID, uuid.UUID | None]]] = [
        [(row.record_id, None) for row in rows] for rows in lexical_row_lists
    ]

    # -- stage 3: RRF fusion, dedupe by record_id, keep top-k -------------------
    fused = rrf_fuse([*dense_lists, *lexical_lists])[: query.k]
    if not fused:
        return []

    # Lexical-only records have no chunk yet — represent them by chunk seq 0.
    async with engine.connect() as conn:
        seq0 = await repo.first_chunk_ids(
            conn, [record_id for record_id, chunk_id, _ in fused if chunk_id is None]
        )
        chunk_ids: list[uuid.UUID] = []
        for record_id, chunk_id, _score in fused:
            chosen = chunk_id if chunk_id is not None else seq0.get(record_id)
            if chosen is not None:
                chunk_ids.append(chosen)
        rows = await repo.hydrate_chunks(conn, chunk_ids, scopes=query.scopes)

    order = {chunk_id: i for i, chunk_id in enumerate(chunk_ids)}
    rows = sorted(rows, key=lambda r: order.get(r.chunk_id, len(order)))
    chunks = [
        RankedChunk(
            chunk_id=row.chunk_id,
            record_id=row.record_id,
            kind=row.kind,
            text=row.text,
            token_count=int(row.token_count),
            importance=float(row.importance),
            decay_score=float(row.decay_score),
        )
        for row in rows
    ]
    if not chunks:
        return []

    # -- stages 4+5: rerank, then the freshness/importance ordering key --------
    # A broken reranker (model failed to load, gateway down) must never fail
    # retrieval: degrade to the fused order (descending positional scores).
    # The NORMALISED query (sub_queries[0]) goes to the reranker so every stage
    # is a function of exactly the packet-cache-key material.
    try:
        raw_scores = await reranker.scores(sub_queries[0], [c.text for c in chunks])
    except Exception:
        logger.exception("memory.rerank_failed_degrading_to_fusion_order")
        raw_scores = [float(len(chunks) - i) for i in range(len(chunks))]
    if len(raw_scores) != len(chunks):
        logger.warning("memory.rerank_score_mismatch", expected=len(chunks), got=len(raw_scores))
        raw_scores = [float(len(chunks) - i) for i in range(len(chunks))]
    return final_order(chunks, raw_scores)


async def _dense_leg(
    qdrant: QdrantIndex,
    embeddings: EmbeddingsClient,
    query: RetrievalQuery,
    sub_queries: list[str],
) -> list[list[tuple[uuid.UUID, uuid.UUID | None]]]:
    """Qdrant top-40 per sub-query on titan_memory — plus titan_kb when kinds
    include procedural (§6.1) — one embeddings call for all sub-queries."""
    response = await embeddings.embed(EmbeddingsRequest(input=sub_queries))
    vectors = [v.embedding for v in response.embeddings]
    collections = ["titan_memory"]
    if "procedural" in query.kinds:
        collections.append("titan_kb")
    dense_filter = _dense_filter(query)
    searches = [
        qdrant.search(collection, vector, limit=DENSE_TOP_K, query_filter=dense_filter)
        for vector in vectors
        for collection in collections
    ]
    results = await asyncio.gather(*searches)
    lists: list[list[tuple[uuid.UUID, uuid.UUID | None]]] = []
    for hits in results:
        ranked: list[tuple[uuid.UUID, uuid.UUID | None]] = []
        for hit in hits:
            payload = hit.get("payload") or {}
            record_raw = payload.get("record_id")
            if record_raw is None:
                continue  # foreign point without provenance — never usable
            ranked.append((uuid.UUID(str(record_raw)), uuid.UUID(str(hit["id"]))))
        lists.append(ranked)
    return lists


async def _lexical_leg(engine: AsyncEngine, query: RetrievalQuery, text_query: str) -> Any:
    async with engine.connect() as conn:
        return await repo.lexical_search(
            conn,
            query=text_query,
            scopes=query.scopes,
            kinds=list(query.kinds) or None,
            limit=LEXICAL_TOP_K,
        )


# --- rerankers ----------------------------------------------------------------


class NoopReranker:
    """RERANKER=off: a zero-latency pass-through — descending positional scores
    hand the fused RRF order to the §6.1 stage-5 key, where decay/importance
    modulate it exactly as they do in every other mode (a fresh, important
    record can still overtake a near-neighbour; that is the stage's purpose).
    The only mode that fits the TITAN-BRN-010 reranker-off budget of ≤400 ms
    (an LLM round-trip cannot); the §6.1 LLM listwise fallback remains
    available as RERANKER=llm."""

    async def scores(self, query: str, texts: list[str]) -> list[float]:
        return [float(len(texts) - i) for i in range(len(texts))]


class CrossEncoderReranker:
    """The §6.1 primary reranker: bge-reranker-v2-m3 cross-encoder, in-process
    via ``sentence-transformers`` (CPU, ONNX backend; install the optional
    ``rerank`` extra). The heavyweight import and model load happen lazily on
    first use, off the event loop (main.py warms it at startup so no user
    request pays the load). ``onnx_file`` selects an explicit weight file —
    e.g. an int8 export baked into the image (§6.1/§6.3); without one the
    library may auto-export, which Phase 37's packaging eliminates.
    ``score_fn`` is injectable so the pipeline logic is testable without the
    model download."""

    MODEL = "BAAI/bge-reranker-v2-m3"

    def __init__(self, score_fn: "ScoreFn | None" = None, *, onnx_file: str | None = None) -> None:
        self._score_fn: ScoreFn | None = score_fn
        self._onnx_file = onnx_file
        self._load_lock = asyncio.Lock()

    def _load(self) -> "ScoreFn":  # pragma: no cover — exercised only with the extra
        from sentence_transformers import CrossEncoder  # noqa: PLC0415

        kwargs: dict[str, Any] = {"backend": "onnx"}
        if self._onnx_file:
            kwargs["model_kwargs"] = {"file_name": self._onnx_file}
        model = CrossEncoder(self.MODEL, **kwargs)

        def predict(pairs: list[tuple[str, str]]) -> list[float]:
            return [float(s) for s in model.predict(pairs)]

        return predict

    async def warm_up(self) -> None:  # pragma: no cover — model path is live-only
        """Load the model off the request path (called from app startup)."""
        await self.scores("warm-up", ["warm-up"])

    async def scores(self, query: str, texts: list[str]) -> list[float]:
        if self._score_fn is None:  # pragma: no cover — model path is live-only
            async with self._load_lock:  # concurrent cold calls load ONCE
                if self._score_fn is None:
                    self._score_fn = await asyncio.to_thread(self._load)
        score_fn = self._score_fn
        pairs = [(query, text) for text in texts]
        return [float(s) for s in await asyncio.to_thread(score_fn, pairs)]


class LlmListwiseReranker:
    """RERANKER=llm (§6.1's stated fallback; also the degraded RERANKER=on path
    when the `rerank` extra is missing): ONE listwise chat call through the
    gateway's cheapest configured model. Robust to malformed model output — any
    parse failure degrades to the input order (scores by position), never an
    error in the retrieval hot path."""

    def __init__(self, gateway: Any, model_alias: str | None = None) -> None:
        self._gateway = gateway
        self._alias = model_alias

    async def scores(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        snippets = "\n".join(
            f"[{i}] {text[:300].replace(chr(10), ' ')}" for i, text in enumerate(texts)
        )
        prompt = (
            "Rank the passages by relevance to the query, most relevant first.\n"
            f"Query: {query}\n\nPassages:\n{snippets}\n\n"
            "Answer with ONLY a JSON array of the passage indices in ranked "
            f"order, e.g. [2,0,1]. There are {len(texts)} passages (0-based)."
        )
        fallback = [float(len(texts) - i) for i in range(len(texts))]
        try:
            response: ChatResponse = await self._gateway.chat(
                ChatRequest(
                    messages=[Message(role="user", content=prompt)],
                    model_alias=self._alias,
                    max_tokens=400,  # a JSON index array — never longer
                )
            )
            ranking = _parse_ranking(response.text or "", len(texts))
        except Exception:
            logger.exception("memory.llm_rerank_failed")
            return fallback
        if ranking is None:
            logger.warning("memory.llm_rerank_unparseable")
            return fallback
        scores = [0.0] * len(texts)
        for rank, index in enumerate(ranking):
            scores[index] = float(len(ranking) - rank)
        return scores


def _parse_ranking(text: str, n: int) -> list[int] | None:
    """The first JSON int array in ``text``, repaired to a permutation of
    0..n-1 (missing indices appended in input order); None when unusable."""
    match = re.search(r"\[[\d,\s]*\]", text)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    seen: list[int] = []
    for value in parsed:
        if isinstance(value, int) and 0 <= value < n and value not in seen:
            seen.append(value)
    if not seen:
        return None
    seen.extend(i for i in range(n) if i not in seen)
    return seen


__all__ = [
    "BUDGET_FILL_RATIO",
    "DENSE_TOP_K",
    "FUSED_TOP_K",
    "LEXICAL_TOP_K",
    "MAX_SUB_QUERIES",
    "RRF_K",
    "WEIGHT_DECAY",
    "WEIGHT_IMPORTANCE",
    "WEIGHT_RERANK",
    "CrossEncoderReranker",
    "LlmListwiseReranker",
    "NoopReranker",
    "RankedChunk",
    "Reranker",
    "RetrievalQuery",
    "fill_budget",
    "final_order",
    "identifier_terms",
    "retrieve",
    "rrf_fuse",
    "shape_queries",
]
