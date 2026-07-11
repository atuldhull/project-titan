"""Memory Packet assembly (Vol 5 §6.2, TITAN-BRN-012) — private to `memory`.

Turns the §6.1 ranked chunks into the typed, cited, budget-bounded packet the
orchestrator splices into agent prompts: sections ordered procedural →
semantic → episodic (standards dominate anecdotes, §6.2 rule b), a
non-evictable ``identity_context`` (rule c), a citation for every included
chunk (rule a), and a ``working_summary`` slot (filled by the working-memory
phase). Identical (scope, query) retrievals within 300 s serve from the Redis
packet cache under ``rc:{scope}:{query_hash}`` (§4.5, TITAN-BRN-012).
"""

import contextlib
import hashlib
import importlib.util
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.db import uuid7
from titan.llm.service import ConfigLoader
from titan.memory import repository as repo
from titan.memory import retrieval
from titan.memory.chunker import count_tokens
from titan.memory.ingest import EmbeddingsClient
from titan.memory.qdrant import QdrantIndex
from titan.observability.metrics import MEMORY_PACKET_CACHE

logger = structlog.get_logger("titan.memory")
_TRACER = trace.get_tracer("titan.memory")

PACKET_CACHE_TTL_SECONDS = 300  # §4.5 rc:{scope}:{query_hash}
_KINDS = ("episodic", "semantic", "procedural")


# --- §6.2 DTOs ----------------------------------------------------------------


class AssembledFor(BaseModel):
    """Who the packet is for — attribution only, never part of the cache key."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(min_length=1, max_length=200)
    task_id: uuid.UUID | None = None
    call_seq: int = 0


class PacketRequest(BaseModel):
    """POST /memory/packets — the §6.1 request tuple."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    scope: str = Field(min_length=1)
    kinds: list[str] = Field(default_factory=list)
    k: int = Field(default=retrieval.FUSED_TOP_K, ge=1, le=50)
    budget_tokens: int = Field(default=4000, ge=256, le=32_000)
    # TITAN-BRN-022: KB documents forced into the packet by slug, independent of
    # similarity ranking (e.g. the code-review step always pins its
    # coding_standard docs). Pinned chunks are NEVER evicted by ranking.
    pins: list[str] = Field(default_factory=list, max_length=20)
    # §9.2: include DEMOTED records (a deep-memory query); default excludes them.
    deep_search: bool = False
    assembled_for: AssembledFor | None = None

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        from titan.memory.service import parse_scope  # noqa: PLC0415 — cycle guard

        parse_scope(value)
        return value

    @field_validator("kinds")
    @classmethod
    def _validate_kinds(cls, value: list[str]) -> list[str]:
        unknown = [kind for kind in value if kind not in _KINDS]
        if unknown:
            raise ValueError(f"kinds must be a subset of {_KINDS}; got {unknown}")
        return value


class PacketCitation(BaseModel):
    """Provenance for one included chunk (§6.2 rule a). ``compressed`` marks a
    chunk whose full text no longer fit the budget and was replaced by its
    parent record's summary line (§8.2 mechanism 3) — the agent knows more
    detail is one retrieval away, and nothing was dropped silently."""

    model_config = ConfigDict(extra="forbid")

    record_id: uuid.UUID
    chunk_id: uuid.UUID
    score: float
    compressed: bool = False
    # TITAN-BRN-022: this chunk was pinned by slug, not ranked — it is present
    # deterministically and was never subject to eviction.
    pinned: bool = False


class PacketSections(BaseModel):
    """§6.2 sections; ordered procedural → semantic → episodic on render."""

    model_config = ConfigDict(extra="forbid")

    identity_context: str = ""
    procedural: list[str] = Field(default_factory=list)
    semantic: list[str] = Field(default_factory=list)
    episodic: list[str] = Field(default_factory=list)
    working_summary: str = ""


class MemoryPacketOut(BaseModel):
    """The §6.2 Memory Packet, exactly."""

    model_config = ConfigDict(extra="forbid")

    packet_id: uuid.UUID = Field(default_factory=uuid7)
    assembled_for: AssembledFor | None = None
    sections: PacketSections = Field(default_factory=PacketSections)
    citations: list[PacketCitation] = Field(default_factory=list)
    token_cost: int = 0
    budget_tokens: int


# --- cache ---------------------------------------------------------------------


def packet_cache_key(request: PacketRequest) -> str:
    """``rc:{scope}:{query_hash}`` (§4.5). The hash covers everything that
    changes the packet CONTENT — query, kinds, k, budget — but never
    ``assembled_for`` (attribution must not fragment the cache).

    The query is whitespace-normalised but CASE-PRESERVED: retrieval is
    case-sensitive (the ALL-CAPS identifier probes), so 'oauth' and 'OAUTH'
    produce different packets and must never share a cache entry."""
    material = "|".join(
        [
            re.sub(r"\s+", " ", request.query).strip(),
            ",".join(sorted(request.kinds)),
            str(request.k),
            str(request.budget_tokens),
            # Pins change the packet CONTENT (they force docs in), so they are
            # part of the cache key — order-independent (a set of slugs).
            "pins:" + ",".join(sorted(request.pins)),
            # deep_search changes which records are eligible (demoted included),
            # so a deep packet must never share a cache entry with a shallow one.
            "deep" if request.deep_search else "shallow",
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"rc:{request.scope}:{digest}"


# --- assembly --------------------------------------------------------------------


async def assemble_packet(
    engine: AsyncEngine,
    redis: Any,
    qdrant: QdrantIndex,
    embeddings: EmbeddingsClient,
    reranker: retrieval.Reranker,
    request: PacketRequest,
) -> MemoryPacketOut:
    """§6.1 stage 6: run retrieval and assemble the cited packet, via the
    300 s Redis cache (TITAN-BRN-012). The assembly span carries the §14
    observability facts — packet_id, per-leg candidate counts, rerank latency
    — metadata only, never packet text (TITAN-OPS-016)."""
    with _TRACER.start_as_current_span("memory.packet_assembly") as span:
        packet = await _assemble_packet(engine, redis, qdrant, embeddings, reranker, request, span)
        span.set_attribute("titan.memory.packet_id", str(packet.packet_id))
        span.set_attribute("titan.memory.token_cost", packet.token_cost)
        span.set_attribute(
            "titan.memory.compressed_citations",
            sum(1 for c in packet.citations if c.compressed),
        )
        return packet


async def _assemble_packet(
    engine: AsyncEngine,
    redis: Any,
    qdrant: QdrantIndex,
    embeddings: EmbeddingsClient,
    reranker: retrieval.Reranker,
    request: PacketRequest,
    span: Any,
) -> MemoryPacketOut:
    key = packet_cache_key(request)
    try:
        cached = await redis.get(key)
    except Exception:
        # The cache is an accelerator, never a dependency: a Redis outage
        # degrades to assembling every packet fresh (TITAN-BRN-012 is SHOULD).
        logger.exception("memory.packet_cache_read_failed")
        cached = None
    if cached is not None:
        try:
            packet = MemoryPacketOut.model_validate_json(cached)
        except Exception as exc:
            # A corrupt or schema-skewed entry (rolling deploy, manual SET) must
            # behave like a miss, not 500 every request until the TTL expires.
            # No exc_info: a pydantic error embeds input_value — cached PACKET
            # CONTENT — and logs carry metadata only (TITAN-OPS-016).
            logger.warning("memory.packet_cache_entry_invalid", error=type(exc).__name__)
            with contextlib.suppress(Exception):
                await redis.delete(key)
            cached = None
        else:
            MEMORY_PACKET_CACHE.labels(result="hit").inc()
            span.set_attribute("titan.memory.cache", "hit")
            packet.assembled_for = request.assembled_for  # attribution is per-caller
            return packet
    MEMORY_PACKET_CACHE.labels(result="miss").inc()
    span.set_attribute("titan.memory.cache", "miss")

    identity = await _identity_context(engine, request.scope)
    identity_tokens = count_tokens(identity)
    # TITAN-BRN-022 — pin-by-slug: the current version's KB chunks, read straight
    # from PostgreSQL (deterministic, no ranking, available even before the
    # chunks finish embedding). Charged right after identity_context and never
    # evicted; a pinned chunk that ALSO ranks is not double-counted.
    pinned = await _pinned_chunks(engine, request)
    pinned_ids = {row.chunk_id for row in pinned}
    pinned_tokens = sum(int(row.token_count) for row in pinned)
    span.set_attribute("titan.memory.pinned", len(pinned))
    stats = retrieval.RetrievalStats()
    ranked = await retrieval.retrieve(
        engine,
        qdrant,
        embeddings,
        reranker,
        retrieval.RetrievalQuery(
            query=request.query,
            scope=request.scope,
            kinds=tuple(request.kinds),
            k=request.k,
            budget_tokens=request.budget_tokens,
            deep_search=request.deep_search,
        ),
        stats=stats,
    )
    span.set_attribute("titan.memory.dense_candidates", stats.dense_candidates)
    span.set_attribute("titan.memory.lexical_candidates", stats.lexical_candidates)
    span.set_attribute("titan.memory.fused", stats.fused)
    span.set_attribute("titan.memory.hydrated", stats.hydrated)
    span.set_attribute("titan.memory.rerank_ms", round(stats.rerank_ms, 2))
    # A pinned chunk that also came back from ranking must not appear twice.
    ranked = [chunk for chunk in ranked if chunk.chunk_id not in pinned_ids]
    # identity_context AND pins are charged FIRST and never evicted (§6.2 rule c,
    # TITAN-BRN-022); ranking fills only what budget remains after them.
    kept = retrieval.fill_budget(
        ranked, request.budget_tokens, reserved=identity_tokens + pinned_tokens
    )

    sections = PacketSections(identity_context=identity)
    citations: list[PacketCitation] = []
    used = identity_tokens + pinned_tokens
    for row in pinned:
        _section_for(sections, str(row.kind)).append(str(row.text))
        citations.append(
            PacketCitation(record_id=row.record_id, chunk_id=row.chunk_id, score=1.0, pinned=True)
        )
    for chunk in kept:
        _section_for(sections, chunk.kind).append(chunk.text)
        citations.append(
            PacketCitation(
                record_id=chunk.record_id,
                chunk_id=chunk.chunk_id,
                score=round(chunk.final_score, 6),
            )
        )
        used += chunk.token_count
    # §8.2 mechanism 3 — compression-on-retrieval: ranked chunks the budget
    # evicted are NOT silently dropped; each becomes its parent record's
    # summary line, marked compressed, while the 85 % fill limit still holds.
    limit = int(request.budget_tokens * retrieval.BUDGET_FILL_RATIO)
    for chunk in ranked[len(kept) :]:
        line = f"[compressed] {chunk.title} (record {chunk.record_id} — retrieve for detail)"
        cost = count_tokens(line)
        if used + cost > limit:
            break
        _section_for(sections, chunk.kind).append(line)
        citations.append(
            PacketCitation(
                record_id=chunk.record_id,
                chunk_id=chunk.chunk_id,
                score=round(chunk.final_score, 6),
                compressed=True,
            )
        )
        used += cost
    packet = MemoryPacketOut(
        assembled_for=request.assembled_for,
        sections=sections,
        citations=citations,
        token_cost=used,
        budget_tokens=request.budget_tokens,
    )
    # Cache WITHOUT attribution; each hit re-attaches its own assembled_for.
    cacheable = packet.model_copy(update={"assembled_for": None})
    try:
        await redis.set(key, cacheable.model_dump_json(), ex=PACKET_CACHE_TTL_SECONDS)
    except Exception:
        logger.exception("memory.packet_cache_write_failed")
    return packet


async def _pinned_chunks(engine: AsyncEngine, request: PacketRequest) -> list[Any]:
    """The current KB chunks for the request's pinned slugs (TITAN-BRN-022),
    scope-isolated to org + the caller's project (TITAN-BRN-003). Empty when
    nothing is pinned or a slug has no active version."""
    if not request.pins:
        return []
    scopes = ("org",) if request.scope == "org" else ("org", request.scope)
    async with engine.connect() as conn:
        rows = await repo.kb_current_chunks_for_slugs(conn, request.pins, scopes=scopes)
    return list(rows)


def _section_for(sections: PacketSections, kind: str) -> list[str]:
    if kind == "procedural":
        return sections.procedural
    if kind == "semantic":
        return sections.semantic
    return sections.episodic


async def _identity_context(engine: AsyncEngine, scope: str) -> str:
    """The non-evictable identity block (§6.2 rule c): the project one-pager
    for project scope (§8.2's richer version lands with working memory)."""
    from titan.memory.service import parse_scope  # noqa: PLC0415 — cycle guard

    project_id = parse_scope(scope)
    if project_id is None:
        return "Context scope: organization-wide memory."
    async with engine.connect() as conn:
        row = await repo.project_identity(conn, project_id)
    if row is None:
        return f"Context scope: project {project_id} (no one-pager on file)."
    return f"Project {row.display_name}: {row.brief}"


# --- reranker factory --------------------------------------------------------------


def cheapest_chat_alias(models_config_path: str) -> str | None:
    """The cheapest configured CHAT model alias (§6.1: the RERANKER=llm listwise
    rerank runs on it). Embedding aliases (dimensions set) are excluded; a
    missing price counts as free (local models win on a laptop)."""
    config = ConfigLoader(Path(models_config_path)).get()
    chat = {
        alias: model for alias, model in config.model_aliases.items() if model.dimensions is None
    }
    if not chat:
        return None

    def input_price(alias: str) -> tuple[Decimal, str]:
        price = chat[alias].price
        return (price.input_per_mtok if price is not None else Decimal(0), alias)

    return min(chat, key=input_price)


def _cross_encoder_deps_present() -> bool:
    """CrossEncoder(backend="onnx") needs sentence-transformers AND its onnx
    stack — a partial install must degrade at BUILD time (to the LLM path),
    never 500 or error-spam at request time. ``optimum.onnxruntime`` is the
    actual import the backend performs, so probe the SUBMODULE — a bare
    ``optimum`` namespace package passes find_spec without it."""
    try:
        return all(
            importlib.util.find_spec(module) is not None
            for module in ("sentence_transformers", "optimum.onnxruntime", "onnxruntime")
        )
    except (ImportError, ModuleNotFoundError):
        return False  # find_spec on a submodule imports the parent; it may fail


def build_reranker(settings: Any, gateway: Any) -> retrieval.Reranker:
    """The mode switch (§6.1 stage 4, §15 R4). RERANKER=on → in-process
    cross-encoder (optional ``rerank`` extra; a missing/partial install degrades
    to the LLM path with a warning). RERANKER=llm → listwise rerank through the
    gateway's cheapest chat model (§6.1's stated fallback). RERANKER=off → a
    zero-latency rank-preserving no-op — the only mode inside TITAN-BRN-010's
    ≤400 ms reranker-off budget. All three are first-class, tested paths."""
    if settings.reranker == "off":
        return retrieval.NoopReranker()
    if settings.reranker == "on":
        if _cross_encoder_deps_present():
            return retrieval.CrossEncoderReranker(
                model=settings.reranker_model,
                onnx_file=settings.reranker_onnx_file,
                max_length=settings.reranker_max_length,
            )
        logger.warning(
            "memory.reranker_extra_missing",
            detail="RERANKER=on but the `rerank` extra (sentence-transformers[onnx]) "
            "is not fully installed; falling back to the LLM listwise reranker",
        )
    try:
        alias = cheapest_chat_alias(settings.models_config_path)
    except Exception:
        # Config unreadable at boot (the gateway itself loads it lazily) —
        # rerank calls then use the gateway's default routing.
        logger.warning("memory.cheapest_alias_unavailable")
        alias = None
    return retrieval.LlmListwiseReranker(gateway, alias)


__all__ = [
    "PACKET_CACHE_TTL_SECONDS",
    "AssembledFor",
    "MemoryPacketOut",
    "PacketCitation",
    "PacketRequest",
    "PacketSections",
    "assemble_packet",
    "build_reranker",
    "cheapest_chat_alias",
    "packet_cache_key",
]
