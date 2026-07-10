"""TITAN-BRN-010 performance harness: 100k-chunk corpus, query→packet p95.

Seeds a synthetic-but-realistic corpus (records + chunks in PostgreSQL, points
in Qdrant — the §5.1 pipeline's exact shapes via ``build_point``), then runs
distinct queries end-to-end through ``assemble_packet`` against REAL services
and reports p50/p95/p99. Targets (reference laptop profile): p95 ≤ 800 ms with
the reranker on, ≤ 400 ms with RERANKER=off. The packet cache is bypassed by
construction (every timed query is unique + Redis is flushed first); the
TITAN-BRN-012 cache path is then asserted separately — a repeated (scope,
query) call must return < 20 ms and increment the cache_hit counter.

Honesty notes:
* Query embeddings are computed by a deterministic in-process stub (the local
  Ollama embedder is not part of this phase's containers); its cost is charged
  as ``--embed-latency-ms`` of asyncio sleep (default 25 ms, the mid-range of
  local CPU embedding — Windows timer granularity typically OVER-charges this).
* RERANKER=on runs the REAL int8 ONNX cross-encoder in-process — run inside a
  venv with the ``rerank`` extra (the seeding fast path also wants numpy,
  which that venv has). RERANKER=llm has no TITAN-BRN-010 target (gateway
  latency dominates) and is not benched.

Usage (from apps/api, against a DISPOSABLE database):
    TITAN_DATABASE_URL=... TITAN_REDIS_URL=... TITAN_QDRANT_URL=... \\
    python scripts/bench_retrieval.py --mode off --wipe
    python scripts/bench_retrieval.py --mode on --skip-seed \\
        --reranker-model D:/titan/.models/ms-marco-MiniLM-L6-v2 \\
        --onnx-file onnx/model_qint8_avx512_vnni.onnx
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import random
import sys
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import httpx
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine

from titan import clients
from titan.llm.service import EmbeddingsRequest, EmbeddingsResponse, EmbeddingVector, Usage
from titan.memory.ingest import build_point
from titan.memory.packets import PacketRequest, assemble_packet
from titan.memory.qdrant import VECTOR_SIZE, QdrantError, QdrantIndex
from titan.memory.retrieval import CrossEncoderReranker, NoopReranker
from titan.settings import Settings

CHUNKS_PER_RECORD = 8
PROJECT_SCOPE_SHARE = 5  # every 5th record sits in the bench project's scope
BENCH_PROJECT = uuid.UUID("00000000-0000-4000-8000-00000000b37b")
TARGETS_MS = {"on": 800.0, "off": 400.0}
CACHE_HIT_BUDGET_MS = 20.0

_VOCAB = [
    "gateway",
    "retry",
    "backoff",
    "socket",
    "timeout",
    "provider",
    "checkout",
    "ledger",
    "invoice",
    "refund",
    "webhook",
    "signature",
    "partner",
    "sandbox",
    "merchant",
    "catalog",
    "import",
    "mapping",
    "preview",
    "queue",
    "worker",
    "saturation",
    "fulfillment",
    "reservation",
    "sweeper",
    "inventory",
    "postgres",
    "failover",
    "replica",
    "vacuum",
    "quorum",
    "redis",
    "sentinel",
    "eviction",
    "counters",
    "kafka",
    "broker",
    "partition",
    "lag",
    "storage",
    "registry",
    "certificate",
    "deploy",
    "rollback",
    "conversion",
    "canary",
    "budget",
    "flag",
    "rollout",
    "percentage",
    "capture",
    "settlement",
    "reconciliation",
    "discrepancy",
    "index",
    "bloat",
    "migration",
    "backfill",
    "checkpoint",
    "snapshot",
    "restore",
    "drill",
    "telemetry",
    "retention",
    "purge",
    "tracing",
    "span",
    "profiler",
    "flamegraph",
    "shard",
    "residency",
    "cluster",
    "quota",
    "bucket",
    "throttle",
    "captcha",
    "limiter",
    "credential",
    "token",
    "rotation",
    "scanner",
]


def _unit_vector(seed_text: str, rng_pool: Any) -> list[float]:
    """Deterministic 768-d unit vector; numpy when available (seeding 100k
    vectors in pure Python costs minutes), pure Python otherwise."""
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    if rng_pool is not None:  # numpy fast path
        gen = rng_pool.random.default_rng(int.from_bytes(digest[:8], "big"))
        vec = gen.standard_normal(VECTOR_SIZE)
        vec /= (vec**2).sum() ** 0.5 or 1.0
        return [round(float(x), 5) for x in vec]
    rng = random.Random(digest)
    raw = [rng.gauss(0.0, 1.0) for _ in range(VECTOR_SIZE)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [round(x / norm, 5) for x in raw]


class DeterministicEmbeddings:
    """EmbeddingsClient stub charging a configurable embed-hop latency."""

    def __init__(self, latency_ms: float, rng_pool: Any) -> None:
        self.latency_ms = latency_ms
        self._pool = rng_pool

    async def embed(self, req: EmbeddingsRequest) -> EmbeddingsResponse:
        if self.latency_ms > 0:
            await asyncio.sleep(self.latency_ms / 1000.0)
        return EmbeddingsResponse(
            embeddings=[
                EmbeddingVector(index=i, embedding=_unit_vector(t, self._pool))
                for i, t in enumerate(req.input)
            ],
            dimensions=VECTOR_SIZE,
            provider="bench",
            model_id="hash-embed-1",
            usage=Usage(input_tokens=sum(len(t.split()) for t in req.input)),
            cost_usd=Decimal("0"),
            latency_ms=int(self.latency_ms),
        )


def _record_meta(index: int) -> tuple[str, str, str, list[str]]:
    """(marker, kind, scope, topic words) for record #index — deterministic."""
    rng = random.Random(index * 7919)
    marker = f"BENCHMARK_{index:06d}"
    kind = ("semantic", "episodic", "procedural")[index % 3]
    scope = f"project:{BENCH_PROJECT}" if index % PROJECT_SCOPE_SHARE == 0 else "org"
    topics = rng.sample(_VOCAB, 8)
    return marker, kind, scope, topics


def _chunk_text(index: int, seq: int, marker: str, kind: str, scope: str, topics: list[str]) -> str:
    """~280-word chunk with the production contextual-header shape — long
    enough that the reranker pays its REAL truncated-sequence cost."""
    rng = random.Random(index * 104729 + seq)
    sentences = [f"[{scope.split(':')[0]} | {kind} | doc {index} | chunk {seq + 1}/8]"]
    sentences.append(f"Reference {marker}: {' '.join(topics[:4])} handling notes.")
    for _ in range(22):
        words = rng.choices(_VOCAB, k=10) + [rng.choice(topics)]
        rng.shuffle(words)
        sentences.append(" ".join(words) + ".")
    return " ".join(sentences)


async def _seed(
    engine: AsyncEngine, qdrant: QdrantIndex, total_chunks: int, rng_pool: Any, wipe: bool
) -> None:
    records = total_chunks // CHUNKS_PER_RECORD
    async with engine.begin() as conn:
        if wipe:
            await conn.execute(
                text("TRUNCATE brain.memory_chunks, brain.memory_records, brain.ingest_outbox")
            )
        existing = (await conn.execute(text("SELECT count(*) FROM brain.memory_records"))).scalar()
    if existing:
        raise SystemExit(
            f"database already holds {existing} records; use --skip-seed to reuse "
            "the corpus or --wipe to replace it (bench wants a DISPOSABLE db)"
        )
    await qdrant.ensure_collections()
    # PG and Qdrant must be wiped TOGETHER: point ids are deterministic, so a
    # reseed at a smaller --corpus-chunks would otherwise leave stale points
    # behind — the bench would then search a larger index than it reports and
    # feed hydration record_ids that no longer exist.
    stale = await qdrant.count("titan_memory")
    if stale and not wipe:
        raise SystemExit(
            f"qdrant already holds {stale} titan_memory points; re-run with --wipe "
            "(bench wants a DISPOSABLE stack)"
        )
    if stale:
        print(f"  wiping {stale} stale qdrant points...")
        ids = sorted(await qdrant.list_point_ids("titan_memory"))
        for start in range(0, len(ids), 500):
            await qdrant.delete_points("titan_memory", ids[start : start + 500])
    started = time.perf_counter()
    for start in range(0, records, 500):
        batch = list(range(start, min(start + 500, records)))
        metas = [_record_meta(i) for i in batch]
        record_ids = [uuid.uuid5(uuid.NAMESPACE_URL, f"bench-record-{i}") for i in batch]
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO brain.memory_records "
                    "(id, kind, scope, source_agent, title, body_md, importance, decay_score) "
                    "SELECT * FROM unnest("
                    "CAST(:ids AS uuid[]), CAST(:kinds AS text[]), CAST(:scopes AS text[]), "
                    "CAST(:agents AS text[]), CAST(:titles AS text[]), CAST(:bodies AS text[]), "
                    "CAST(:imps AS real[]), CAST(:decays AS real[]))"
                ),
                {
                    "ids": [str(r) for r in record_ids],
                    "kinds": [m[1] for m in metas],
                    "scopes": [m[2] for m in metas],
                    "agents": ["bench" for _ in batch],
                    "titles": [
                        f"bench doc {i} ({m[0]})" for i, m in zip(batch, metas, strict=True)
                    ],
                    "bodies": [f"{m[0]} {' '.join(m[3])}" for m in metas],
                    "imps": [0.2 + (i % 10) * 0.07 for i in batch],
                    "decays": [0.5 + (i % 11) * 0.045 for i in batch],
                },
            )
        chunk_rows: list[tuple[uuid.UUID, uuid.UUID, int, str]] = []
        points: list[dict[str, Any]] = []
        now = datetime.now(tz=UTC)
        for i, record_id, meta in zip(batch, record_ids, metas, strict=True):
            marker, kind, scope, topics = meta
            for seq in range(CHUNKS_PER_RECORD):
                chunk_id = uuid.uuid5(uuid.NAMESPACE_URL, f"bench-chunk-{i}-{seq}")
                body = _chunk_text(i, seq, marker, kind, scope, topics)
                chunk_rows.append((chunk_id, record_id, seq, body))
                # build_point wants a Row but only reads attributes — a
                # namespace with the exact chunk_for_embedding shape is fine.
                row = SimpleNamespace(
                    chunk_id=chunk_id,
                    record_id=record_id,
                    scope=scope,
                    kind=kind,
                    source_agent="bench",
                    created_at=now,
                    metadata={},
                    decay_score=0.5 + (i % 11) * 0.045,
                    status="active",
                    project_id=BENCH_PROJECT if scope != "org" else None,
                )
                points.append(build_point(cast("Row[Any]", row), _unit_vector(body, rng_pool)))
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO brain.memory_chunks "
                    "(id, record_id, seq, text, token_count, embed_model, embedded_at) "
                    "SELECT t.id, t.record_id, t.seq, t.text, t.tokens, 'hash-embed-1', now() "
                    "FROM unnest(CAST(:ids AS uuid[]), CAST(:rids AS uuid[]), "
                    "CAST(:seqs AS int[]), CAST(:texts AS text[]), CAST(:tokens AS int[])) "
                    "AS t(id, record_id, seq, text, tokens)"
                ),
                {
                    "ids": [str(c[0]) for c in chunk_rows],
                    "rids": [str(c[1]) for c in chunk_rows],
                    "seqs": [c[2] for c in chunk_rows],
                    "texts": [c[3] for c in chunk_rows],
                    "tokens": [len(c[3].split()) for c in chunk_rows],
                },
            )
        for p_start in range(0, len(points), 256):
            batch_points = points[p_start : p_start + 256]
            # Upserts are id-keyed and idempotent, so a transport flake
            # (Docker proxy reset) is safely retried.
            for attempt in range(4):
                try:
                    await qdrant.upsert_points("titan_memory", batch_points)
                    break
                except QdrantError:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(1.0 + attempt)
        done = (start + len(batch)) * CHUNKS_PER_RECORD
        print(f"  seeded {done}/{total_chunks} chunks ({time.perf_counter() - started:.0f}s)")
    final = await qdrant.count("titan_memory")
    if final != records * CHUNKS_PER_RECORD:
        raise SystemExit(f"seed mismatch: qdrant holds {final} points, expected {total_chunks}")


async def _wait_for_index(http: Any, timeout_s: float = 900.0) -> None:
    """Block until Qdrant finishes optimizing/indexing the bulk upload —
    benching against unindexed segments would measure full scans, not HNSW."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = await http.get("/collections/titan_memory")
        payload = response.json()["result"]
        if payload["status"] == "green":
            return
        await asyncio.sleep(2.0)
    raise SystemExit("qdrant did not reach status=green in time")


def _queries(count: int, records: int) -> list[tuple[str, str, list[str]]]:
    """(query, scope, kinds) — every query text UNIQUE (cache never hits) and
    every word drawn from its target record (websearch_to_tsquery ANDs terms)."""
    rng = random.Random(42)
    picked = rng.sample(range(records), min(count, records))
    out: list[tuple[str, str, list[str]]] = []
    for n, i in enumerate(picked):
        marker, kind, scope, topics = _record_meta(i)
        query_scope = scope if scope != "org" else "org"
        if n % 5 == 4:
            query = f"what do we know about {marker}"
        elif n % 2 == 0:
            query = f"{marker} details"
        else:
            query = " ".join(rng.sample(topics, 4))
        kinds = [kind] if n % 5 == 2 else []
        out.append((query, query_scope, kinds))
    return out


def _percentile(sorted_ms: list[float], q: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(q * (len(sorted_ms) - 1) + 0.5))]


def _hits() -> float:
    value = REGISTRY.get_sample_value("titan_memory_packet_cache_total", {"result": "hit"})
    return value or 0.0


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    # Optional seeding fast path (present in the rerank venv via onnxruntime).
    numpy: Any = importlib.import_module("numpy") if importlib.util.find_spec("numpy") else None
    engine = clients.create_engine(settings)
    redis = clients.create_redis(settings)
    # Not clients.create_http_client: its 5 s timeout suits readiness pings,
    # while bulk upserts of 256x768-float points can stall behind a segment
    # flush. keepalive_expiry=0: the Docker Desktop port proxy resets reused
    # connections under rapid multi-MB PUTs (surfaces as an empty ReadError).
    http = httpx.AsyncClient(
        base_url=settings.qdrant_url,
        timeout=httpx.Timeout(120.0),
        limits=httpx.Limits(max_keepalive_connections=0),
    )
    qdrant = QdrantIndex(http)
    embeddings = DeterministicEmbeddings(args.embed_latency_ms, numpy)
    if args.mode == "on":
        reranker: Any = CrossEncoderReranker(
            model=args.reranker_model or settings.reranker_model,
            onnx_file=args.onnx_file or settings.reranker_onnx_file,
            max_length=settings.reranker_max_length,
        )
    else:
        reranker = NoopReranker()

    try:
        if not args.skip_seed:
            print(f"seeding {args.corpus_chunks} chunks...")
            await _seed(engine, qdrant, args.corpus_chunks, numpy, args.wipe)
        else:
            # --skip-seed must not become a guard bypass: a half-seeded corpus
            # (crash mid-seed, or a different --corpus-chunks than was seeded)
            # would bench lexical-only retrieval while claiming a 100k-point
            # HNSW — a false PASS. Both stores must hold EXACTLY the corpus.
            q_count = await qdrant.count("titan_memory")
            async with engine.connect() as conn:
                pg_chunks = (
                    await conn.execute(text("SELECT count(*) FROM brain.memory_chunks"))
                ).scalar()
            if q_count != args.corpus_chunks or pg_chunks != args.corpus_chunks:
                raise SystemExit(
                    f"--skip-seed corpus mismatch: qdrant={q_count} pg={pg_chunks} "
                    f"expected={args.corpus_chunks}; reseed with --wipe"
                )
        print("waiting for qdrant indexing...")
        await _wait_for_index(http)
        # Cache starts COLD (every timed call is a miss) — by deleting ONLY the
        # rc:* packet-cache keys. A flushdb would nuke the whole logical DB
        # (ingest stream, consumer groups, DLQ evidence) if someone points the
        # bench at a live stack's Redis.
        cleared = 0
        async for key in redis.scan_iter(match="rc:*", count=500):
            await redis.delete(key)
            cleared += 1
        if cleared:
            print(f"cleared {cleared} cached packets")

        if args.mode == "on":
            print("warming up the cross-encoder (model load off the timed path)...")
            await reranker.warm_up()

        records = args.corpus_chunks // CHUNKS_PER_RECORD
        queries = _queries(args.queries + args.warmup, records)
        timings: list[float] = []
        empty = 0
        for n, (query, scope, kinds) in enumerate(queries):
            request = PacketRequest(query=query, scope=scope, kinds=kinds)
            t0 = time.perf_counter()
            packet = await assemble_packet(engine, redis, qdrant, embeddings, reranker, request)
            elapsed = (time.perf_counter() - t0) * 1000.0
            if n >= args.warmup:
                timings.append(elapsed)
                if not packet.citations:
                    empty += 1
        timings.sort()
        target = TARGETS_MS[args.mode]
        p50, p95, p99 = (_percentile(timings, q) for q in (0.50, 0.95, 0.99))
        summary = {
            "mode": args.mode,
            "corpus_chunks": args.corpus_chunks,
            "queries": len(timings),
            "embed_latency_ms_charged": args.embed_latency_ms,
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "p99_ms": round(p99, 1),
            "p95_target_ms": target,
            "empty_packets": empty,
        }
        ok = p95 <= target and empty <= len(timings) // 10

        # --- TITAN-BRN-012: the cache path, asserted separately ---------------
        cache_query, cache_scope, cache_kinds = queries[-1]
        request = PacketRequest(query=cache_query, scope=cache_scope, kinds=cache_kinds)
        await assemble_packet(engine, redis, qdrant, embeddings, reranker, request)
        hits_before = _hits()
        t0 = time.perf_counter()
        await assemble_packet(engine, redis, qdrant, embeddings, reranker, request)
        cache_ms = (time.perf_counter() - t0) * 1000.0
        cache_ok = cache_ms < CACHE_HIT_BUDGET_MS and _hits() == hits_before + 1
        summary["cache_second_call_ms"] = round(cache_ms, 2)
        summary["cache_hit_counter_incremented"] = _hits() == hits_before + 1
        print(json.dumps(summary, indent=1))
        verdict = "PASS" if ok and cache_ok else "FAIL"
        print(
            f"TITAN-BRN-010 [{args.mode}] p95={p95:.0f}ms target={target:.0f}ms | "
            f"TITAN-BRN-012 hit={cache_ms:.1f}ms (<{CACHE_HIT_BUDGET_MS:.0f}) -> {verdict}"
        )
        print(
            f"NOTE: the query-embedding hop is CHARGED as a flat {args.embed_latency_ms:.0f}ms, "
            "not measured — this gate cannot catch an embed-side regression. Re-measure "
            "end-to-end when the local embedder (Ollama) joins the stack."
        )
        return 0 if ok and cache_ok else 1
    finally:
        await redis.aclose()
        await http.aclose()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="TITAN-BRN-010/012 retrieval benchmark")
    parser.add_argument("--mode", choices=["on", "off"], required=True)
    parser.add_argument("--corpus-chunks", type=int, default=100_000)
    parser.add_argument("--queries", type=int, default=120)
    parser.add_argument("--warmup", type=int, default=8)
    parser.add_argument("--embed-latency-ms", type=float, default=25.0)
    parser.add_argument("--skip-seed", action="store_true", help="reuse the seeded corpus")
    parser.add_argument("--wipe", action="store_true", help="truncate brain tables first")
    parser.add_argument("--reranker-model", default=None, help="model dir/id (mode=on)")
    parser.add_argument("--onnx-file", default=None, help="onnx file within the model dir")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
