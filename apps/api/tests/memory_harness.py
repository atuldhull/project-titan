"""Shared §6 retrieval test harness (Phases 36–37).

``Pipeline`` runs the full memory path — real PostgreSQL schema, fake
Qdrant/embeddings/redis, a chosen reranker mode — so packet tests and the
TITAN-BRN-011 eval gate drive byte-identical production code hermetically.
Extracted from ``test_memory_packets.py`` when Phase 37's eval gate became the
second consumer.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from typing import Any

import fakeredis.aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.memory_fakes import FakeQdrantServer, StubEmbeddings
from titan.events.service import RedisStreamBus
from titan.memory import service as memory
from titan.memory.ingest import EMBEDDERS_GROUP, STREAM_MEMORY_INGEST, process_ingest_batch
from titan.memory.packets import MemoryPacketOut, PacketRequest, assemble_packet
from titan.memory.qdrant import QdrantIndex
from titan.memory.retrieval import CrossEncoderReranker, LlmListwiseReranker, NoopReranker
from titan.memory.service import IngestRelay, MemoryEmbedder, MemoryEvent, MemoryEventBatch

_WORDS = re.compile(r"\w+")


def overlap_scorer(pairs: list[tuple[str, str]]) -> list[float]:
    """Deterministic stand-in for the cross-encoder: IDF-weighted token
    overlap. Document frequency is computed across THIS call's candidate texts,
    so a rare token (an identifier appearing in one candidate) dominates and
    stopword overlap barely counts — the way a real cross-encoder scores a
    distinctive match far above 'we/about/the' co-occurrence. A plain unweighted
    overlap misranks natural-language probes ('what do we know about EVL_X'):
    five prose tokens drown the one identifier that matters."""
    texts = [chunk_text for _query, chunk_text in pairs]
    n = max(1, len(texts))
    token_sets: list[set[str]] = []
    df: dict[str, int] = {}
    for chunk_text in texts:
        tokens = {token.lower() for token in _WORDS.findall(chunk_text)}
        token_sets.append(tokens)
        for token in tokens:
            df[token] = df.get(token, 0) + 1

    def idf(token: str) -> float:
        return math.log1p(n / df.get(token, 1))

    scores: list[float] = []
    for (query, _text), t_tokens in zip(pairs, token_sets, strict=True):
        q_tokens = {token.lower() for token in _WORDS.findall(query)}
        denominator = sum(idf(token) for token in q_tokens) or 1.0
        scores.append(sum(idf(token) for token in q_tokens & t_tokens) / denominator)
    return scores


class RankingGateway:
    """Fake gateway for the RERANKER=llm listwise mode: ranks by token overlap,
    replying in the exact JSON-array wire format the prompt requests."""

    async def chat(self, request: Any) -> Any:
        prompt = request.messages[0].content
        assert isinstance(prompt, str)
        query = prompt.split("Query: ", 1)[1].split("\n", 1)[0]
        passages: list[tuple[int, str]] = []
        for line in prompt.splitlines():
            if line.startswith("[") and "]" in line:
                index_text, _, body = line.partition("] ")
                try:
                    passages.append((int(index_text.lstrip("[")), body))
                except ValueError:
                    continue
        scored = overlap_scorer([(query, body) for _i, body in passages])
        order = sorted(range(len(passages)), key=lambda i: -scored[i])
        ranking = [passages[i][0] for i in order]

        class _Resp:
            text = json.dumps(ranking)

        return _Resp()


class Pipeline:
    """The full §6 harness: real PG + fake Qdrant/embeddings/redis + a chosen
    reranker, with helpers to seed and fully ingest records. ``embeddings``
    defaults to :class:`StubEmbeddings` (random unit vectors — wiring tests);
    the eval gate injects :class:`HashingEmbeddings` so the dense leg carries
    real signal."""

    def __init__(self, engine: AsyncEngine, mode: str = "on", *, embeddings: Any = None) -> None:
        self.engine = engine
        self.server = FakeQdrantServer()
        self.qdrant = QdrantIndex(self.server.client())
        self.embeddings = embeddings if embeddings is not None else StubEmbeddings()
        self.redis: Any = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.bus = RedisStreamBus(self.redis)
        self.relay = IngestRelay(self.engine, self.bus)
        self.embedder = MemoryEmbedder(self.engine, self.bus, self.qdrant, self.embeddings)
        if mode == "on":
            self.reranker: Any = CrossEncoderReranker(score_fn=overlap_scorer)
        elif mode == "llm":
            self.reranker = LlmListwiseReranker(RankingGateway())
        else:  # off — the zero-latency rank-preserving no-op
            self.reranker = NoopReranker()

    async def seed(
        self, records: list[dict[str, Any]], *, scope: str = "org", source: str = "memory_agent"
    ) -> dict[str, uuid.UUID]:
        """Write + fully ingest records; returns marker/title -> record_id."""
        batch = MemoryEventBatch(
            source_agent=source,
            events=[
                MemoryEvent(
                    kind=r.get("kind", "semantic"),
                    scope=r.get("scope", scope),
                    title=r["title"],
                    body_md=r["body_md"],
                    importance=r.get("importance", 0.5),
                )
                for r in records
            ],
        )
        async with self.engine.begin() as conn:
            result = await memory.write_events(conn, batch, source_agent=source, actor_agent="test")
        await self.qdrant.ensure_collections()
        await self.bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        for _ in range(max(40, len(records) * 3)):
            published = await self.relay.relay_once()
            handled = await process_ingest_batch(self.embedder, self.bus, "pkt", block_ms=1)
            if not published and not handled:
                break
        return {
            records[i].get("marker", records[i]["title"]): result.records[i].record_id
            for i in range(len(records))
        }

    async def packet(self, **kwargs: Any) -> MemoryPacketOut:
        request = PacketRequest(**kwargs)
        return await assemble_packet(
            self.engine, self.redis, self.qdrant, self.embeddings, self.reranker, request
        )


async def quiesce_outbox(engine: AsyncEngine) -> None:
    """Neutralise other tests' rows on the shared DB (see test_memory_ingest)."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE brain.ingest_outbox SET processed_at = now() WHERE processed_at IS NULL")
        )


def create_project(api: dict[str, Any], name_prefix: str) -> str:
    """A fresh project via the API; returns its id (for project:<id> scopes)."""
    response = api["client"].post(
        "/api/v1/projects",
        json={
            "name": f"{name_prefix}-{uuid.uuid4().hex[:10]}",
            "display_name": f"{name_prefix} project",
            "brief": f"The {name_prefix} project brief.",
        },
        headers=api["token_for"]("admin"),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])
