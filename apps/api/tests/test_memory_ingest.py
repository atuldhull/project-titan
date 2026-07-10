"""The §5.1 embedding-ingestion pipeline (Phase 35; ADR-05-01, TITAN-BRN-006/007)
— relay, embedder, PEL recovery, DLQ, and the §4.4 payload, against the real
schema (fakeredis bus + FakeQdrant wire + deterministic stub embeddings).

DoD coverage: duplicate stream delivery is idempotent; a worker killed mid-batch
loses nothing (PEL recovery); poison dead-letters after MAX_ATTEMPTS deliveries
(= 5 retries per §5.1); no provider SDK import exists anywhere in the Memory
Service (TITAN-BRN-006 grep test)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from tests.memory_fakes import FakeQdrantServer, StubEmbeddings, deterministic_vector
from titan.events.service import RedisStreamBus
from titan.memory import service as memory
from titan.memory.ingest import (
    EMBEDDERS_GROUP,
    INGEST_DLQ,
    MAX_ATTEMPTS,
    STREAM_MEMORY_INGEST,
    IngestRelay,
    MemoryEmbedder,
    backoff_seconds,
    process_ingest_batch,
    reap_ingest_once,
    stable_event_id,
)
from titan.memory.qdrant import QdrantIndex
from titan.memory.service import MemoryEvent, MemoryEventBatch
from titan.observability.metrics import MEMORY_INGEST_LAG

pytestmark = pytest.mark.schema


def _bus() -> tuple[RedisStreamBus, fakeredis.aioredis.FakeRedis]:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return RedisStreamBus(redis), redis


async def _write_event(engine: AsyncEngine, *, title: str, body: str) -> uuid.UUID:
    batch = MemoryEventBatch(
        source_agent="memory_agent",
        events=[
            MemoryEvent(
                kind="semantic",
                scope="org",
                title=title,
                body_md=body,
                metadata={"tags": ["pipeline", "test"]},
            )
        ],
    )
    async with engine.begin() as conn:
        result = await memory.write_events(
            conn, batch, source_agent="memory_agent", actor_agent="test"
        )
    return result.records[0].record_id


async def _quiesce_outbox(engine: AsyncEngine) -> None:
    """Neutralise outbox rows left by OTHER tests on the shared throwaway DB, so
    relay/embed call counts in this test see only this test's rows."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE brain.ingest_outbox SET processed_at = now() WHERE processed_at IS NULL")
        )


async def _unprocessed_outbox(engine: AsyncEngine, record_id: uuid.UUID) -> list[Any]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT o.id, o.chunk_id, o.published_at, o.processed_at "
                "FROM brain.ingest_outbox o JOIN brain.memory_chunks c ON c.id = o.chunk_id "
                "WHERE c.record_id = :rid ORDER BY o.id"
            ),
            {"rid": record_id},
        )
        return list(rows.all())


def _pipeline(
    engine: AsyncEngine, embeddings: StubEmbeddings | None = None
) -> tuple[IngestRelay, MemoryEmbedder, RedisStreamBus, FakeQdrantServer, StubEmbeddings]:
    bus, _redis = _bus()
    server = FakeQdrantServer()
    stub = embeddings if embeddings is not None else StubEmbeddings()
    relay = IngestRelay(engine, bus)
    embedder = MemoryEmbedder(engine, bus, QdrantIndex(server.client()), stub)
    return relay, embedder, bus, server, stub


def _lag_count() -> float:
    for metric in MEMORY_INGEST_LAG.collect():
        for sample in metric.samples:
            if sample.name.endswith("_count"):
                return float(sample.value)
    return 0.0


# --- relay -------------------------------------------------------------------


def test_relay_publishes_each_row_once_with_stable_event_id(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    relay, _embedder, bus, _server, _stub = _pipeline(engine)

    async def scenario() -> tuple[list[Any], list[Any], int]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="relay", body="alpha beta gamma")
        first = await relay.relay_once()
        rows = await _unprocessed_outbox(engine, record_id)
        second = await relay.relay_once()  # nothing new → no republish inside window
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        deliveries = await bus.consume(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP, "c1", block_ms=1)
        assert first >= 1
        return rows, deliveries, second

    rows, deliveries, second = run_async(scenario)
    assert all(row.published_at is not None for row in rows)
    assert second == 0  # freshly-published rows are NOT republished
    by_id = {d.envelope.event_id for d in deliveries}
    assert {stable_event_id(int(row.id)) for row in rows} <= by_id


def test_relay_republishes_stale_unprocessed_rows(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    relay, _embedder, bus, _server, _stub = _pipeline(engine)

    async def scenario() -> int:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="stale", body="stale row body")
        await relay.relay_once()
        # Simulate a wiped Redis: rows published long ago but never processed.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE brain.ingest_outbox SET published_at = now() - interval '1 hour' "
                    "WHERE chunk_id IN (SELECT id FROM brain.memory_chunks "
                    "                   WHERE record_id = :rid)"
                ),
                {"rid": record_id},
            )
        return await relay.relay_once()

    assert run_async(scenario) >= 1  # healed from PostgreSQL (ADR-05-01)


# --- embedder: success path ----------------------------------------------------


def test_embedder_upserts_point_with_4_4_payload_and_stamps(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    relay, embedder, bus, server, stub = _pipeline(engine)
    lag_before = _lag_count()

    async def scenario() -> tuple[uuid.UUID, list[Any], dict[str, Any]]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="payload", body="the payload body")
        await embedder.qdrant.ensure_collections()
        await relay.relay_once()
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        await process_ingest_batch(embedder, bus, "c1", block_ms=1)
        rows = await _unprocessed_outbox(engine, record_id)
        async with engine.connect() as conn:
            chunk = (
                await conn.execute(
                    text(
                        "SELECT id, embedded_at, embed_model FROM brain.memory_chunks "
                        "WHERE record_id = :rid"
                    ),
                    {"rid": record_id},
                )
            ).one()
        points = server.points("titan_memory")
        return record_id, [chunk, rows], points

    record_id, (chunk, rows), points = run_async(scenario)
    # §5.1 success markers, in one transaction.
    assert chunk.embedded_at is not None and chunk.embed_model == "stub-embed-1"
    assert all(row.processed_at is not None for row in rows)
    # Point id == chunk id (ADR-05-01); §4.4 payload from the record.
    point = points[str(chunk.id)]
    payload = point["payload"]
    assert payload["scope"] == "org"
    assert payload["kind"] == "semantic"
    assert payload["record_id"] == str(record_id)
    assert payload["source_agent"] == "memory_agent"
    assert payload["tags"] == ["pipeline", "test"]
    assert payload["decay_score"] == pytest.approx(1.0)
    assert "created_at" in payload and "project_id" not in payload  # org scope → no project
    assert _lag_count() > lag_before  # TITAN-BRN-007 histogram observed


def test_duplicate_delivery_is_idempotent(api: dict[str, Any]) -> None:
    """DoD: duplicate stream delivery must have exactly-once effect."""
    engine: AsyncEngine = api["engine"]
    relay, embedder, bus, server, stub = _pipeline(engine)

    async def scenario() -> tuple[int, int]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="dup", body="duplicate delivery body")
        await embedder.qdrant.ensure_collections()
        await relay.relay_once()
        # Force a stale republish: the SAME outbox rows go onto the stream twice
        # (same stable event ids, two stream entries).
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE brain.ingest_outbox SET published_at = now() - interval '1 hour' "
                    "WHERE chunk_id IN (SELECT id FROM brain.memory_chunks "
                    "                   WHERE record_id = :rid)"
                ),
                {"rid": record_id},
            )
        await relay.relay_once()
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        handled = 0
        while await process_ingest_batch(embedder, bus, "c1", block_ms=1):
            handled += 1
        return stub.calls, server.upsert_calls

    embed_calls, upserts = run_async(scenario)
    # The second delivery hits the processed_at fast-path: no second embed, no
    # second upsert — exactly-once effect from at-least-once delivery.
    assert embed_calls == 1
    assert upserts == 1


def test_bus_level_duplicate_after_ack_is_dropped(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    relay, embedder, bus, _server, stub = _pipeline(engine)

    async def scenario() -> tuple[int, int]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="seen", body="seen dedup body")
        await embedder.qdrant.ensure_collections()
        await relay.relay_once()
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        first = await process_ingest_batch(embedder, bus, "c1", block_ms=1)
        # Re-publish the identical event id AFTER the ack: the bus seen-ledger
        # drops it before the embedder ever sees it. (Scoped to THIS record —
        # the shared test DB carries other tests' outbox rows.)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE brain.ingest_outbox SET published_at = NULL, processed_at = NULL "
                    "WHERE chunk_id IN (SELECT id FROM brain.memory_chunks "
                    "                   WHERE record_id = :rid)"
                ),
                {"rid": record_id},
            )
        await relay.relay_once()
        second = await process_ingest_batch(embedder, bus, "c1", block_ms=1)
        return first, second

    first, second = run_async(scenario)
    assert first >= 1
    assert second == 0  # dedup'd at the bus (TITAN-BCK-010)


# --- crash recovery + DLQ -------------------------------------------------------


def test_killed_worker_mid_batch_loses_nothing(api: dict[str, Any]) -> None:
    """DoD: consume without ack (the crash), then PEL reclaim completes the work."""
    engine: AsyncEngine = api["engine"]
    relay, embedder, bus, server, _stub = _pipeline(engine)

    async def scenario() -> tuple[int, int]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="crash", body="crash recovery body")
        await embedder.qdrant.ensure_collections()
        await relay.relay_once()
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        # Worker A reads the delivery and dies before handling it (no ack).
        died_with = await bus.consume(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP, "dead-worker")
        assert died_with
        # Worker B's reaper reclaims and completes it.
        reclaimed = await reap_ingest_once(embedder, bus, "live-worker", min_idle_ms=0)
        async with engine.connect() as conn:
            embedded = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.memory_chunks "
                        "WHERE record_id = :rid AND embedded_at IS NOT NULL"
                    ),
                    {"rid": record_id},
                )
            ).scalar_one()
        return reclaimed, int(embedded)

    reclaimed, embedded = run_async(scenario)
    assert reclaimed >= 1 and embedded >= 1
    assert server.upsert_calls >= 1  # the vector landed despite the crash


def test_poison_dead_letters_after_max_attempts_and_stays_dead(api: dict[str, Any]) -> None:
    """§5.1: max 5 deliveries fail, then the DLQ — and dead-lettering is
    TERMINAL: the outbox row is stamped dead_at, so even a forced stale window
    republishes NOTHING (no infinite poison loop, no duplicate DLQ entries)."""
    engine: AsyncEngine = api["engine"]
    always_failing = StubEmbeddings(fail_times=10_000)
    relay, embedder, bus, _server, _stub = _pipeline(engine, embeddings=always_failing)

    async def scenario() -> tuple[int, int, int, int, int, int]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="poison", body="poison body")
        await embedder.qdrant.ensure_collections()
        await relay.relay_once()
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        await process_ingest_batch(embedder, bus, "c1", block_ms=1)  # delivery 1 fails
        for _ in range(MAX_ATTEMPTS + 1):  # retries 2..5 fail, then DLQ routing
            await reap_ingest_once(embedder, bus, "c1", min_idle_ms=0)
        redis = bus._redis  # the fake — peek at the streams directly
        dlq_len = int(await redis.xlen(INGEST_DLQ))
        pending = await bus.pending_depth(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        # The quarantine: dead_at stamped, and a forced stale window republishes 0.
        async with engine.connect() as conn:
            dead = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.ingest_outbox o "
                        "JOIN brain.memory_chunks c ON c.id = o.chunk_id "
                        "WHERE c.record_id = :rid AND o.dead_at IS NOT NULL"
                    ),
                    {"rid": record_id},
                )
            ).scalar_one()
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE brain.ingest_outbox SET published_at = now() - interval '1 hour'")
            )
        republished = await relay.relay_once()
        dlq_after = int(await redis.xlen(INGEST_DLQ))
        return always_failing.calls, dlq_len, pending, int(dead), republished, dlq_after

    attempts, dlq_len, pending, dead, republished, dlq_after = run_async(scenario)
    assert dlq_len == 1  # dead-lettered exactly once
    assert pending == 0  # nothing left pending
    assert attempts == MAX_ATTEMPTS  # 5 deliveries processed, 6th claim → DLQ
    assert dead == 1  # quarantine stamped in the canonical store
    assert republished == 0  # the relay never resurrects dead rows
    assert dlq_after == 1  # the DLQ does not accumulate duplicates


def test_transient_qdrant_failure_retries_then_succeeds(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    relay, embedder, bus, server, _stub = _pipeline(engine)

    async def scenario() -> tuple[int, int]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="transient", body="transient qdrant")
        await embedder.qdrant.ensure_collections()
        server.fail_upserts = 1  # first upsert 503s → nack → reclaim succeeds
        await relay.relay_once()
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        await process_ingest_batch(embedder, bus, "c1", block_ms=1)
        await reap_ingest_once(embedder, bus, "c1", min_idle_ms=0)
        async with engine.connect() as conn:
            embedded = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.memory_chunks "
                        "WHERE record_id = :rid AND embedded_at IS NOT NULL"
                    ),
                    {"rid": record_id},
                )
            ).scalar_one()
        return int(embedded), await bus.pending_depth(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)

    embedded, pending = run_async(scenario)
    assert embedded >= 1 and pending == 0


# --- delete op + vanished chunk ---------------------------------------------------


def test_delete_op_removes_the_point(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    relay, embedder, bus, server, _stub = _pipeline(engine)

    async def scenario() -> tuple[int, int]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="del", body="delete me")
        await embedder.qdrant.ensure_collections()
        await relay.relay_once()
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        await process_ingest_batch(embedder, bus, "c1", block_ms=1)
        before = len(server.points("titan_memory"))
        # Enqueue the §5.1 delete op for the chunk (prune phases will do this).
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO brain.ingest_outbox (chunk_id, op) "
                    "SELECT id, 'delete' FROM brain.memory_chunks WHERE record_id = :rid"
                ),
                {"rid": record_id},
            )
        await relay.relay_once()
        await process_ingest_batch(embedder, bus, "c1", block_ms=1)
        return before, len(server.points("titan_memory"))

    before, after = run_async(scenario)
    assert before >= 1 and after == 0


def test_already_processed_delivery_fast_paths_without_embedding(api: dict[str, Any]) -> None:
    """A delivery whose outbox row is already processed_at-stamped (e.g. work
    completed by another consumer between publish and this delivery) is an
    acked no-op: no embed call, no upsert."""
    engine: AsyncEngine = api["engine"]
    relay, embedder, bus, server, stub = _pipeline(engine)

    async def scenario() -> tuple[int, int, int]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="done", body="already done body")
        await embedder.qdrant.ensure_collections()
        await relay.relay_once()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE brain.ingest_outbox SET processed_at = now() WHERE chunk_id IN "
                    "(SELECT id FROM brain.memory_chunks WHERE record_id = :rid)"
                ),
                {"rid": record_id},
            )
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        handled = await process_ingest_batch(embedder, bus, "c1", block_ms=1)
        return handled, stub.calls, server.upsert_calls

    handled, embed_calls, upserts = run_async(scenario)
    assert handled >= 1  # delivery consumed + acked
    assert embed_calls == 0 and upserts == 0  # fast-path: exactly-once effect


def test_upsert_superseded_by_later_delete_never_resurrects(api: dict[str, Any]) -> None:
    """Op-order guard: when a LATER delete op exists in the outbox for the same
    chunk, an interleaved upsert delivery completes as a no-op — parallel
    embedders can never resurrect a deleted point."""
    engine: AsyncEngine = api["engine"]
    relay, embedder, bus, server, stub = _pipeline(engine)

    async def scenario() -> tuple[int, int, int]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="order", body="ordering race body")
        await embedder.qdrant.ensure_collections()
        # Enqueue the later delete BEFORE the upsert is ever processed.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO brain.ingest_outbox (chunk_id, op) "
                    "SELECT id, 'delete' FROM brain.memory_chunks WHERE record_id = :rid"
                ),
                {"rid": record_id},
            )
        await relay.relay_once()
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        # Both deliveries processed (in whatever order the stream yields them).
        while await process_ingest_batch(embedder, bus, "c1", block_ms=1):
            pass
        async with engine.connect() as conn:
            unprocessed = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.ingest_outbox o "
                        "JOIN brain.memory_chunks c ON c.id = o.chunk_id "
                        "WHERE c.record_id = :rid AND o.processed_at IS NULL"
                    ),
                    {"rid": record_id},
                )
            ).scalar_one()
        return stub.calls, len(server.points("titan_memory")), int(unprocessed)

    embed_calls, points, unprocessed = run_async(scenario)
    assert points == 0  # the deleted chunk is NOT resurrected
    assert embed_calls == 0  # the superseded upsert never even embedded
    assert unprocessed == 0  # both ops completed


def test_superseded_precheck_cleans_up_a_crashed_attempts_point(api: dict[str, Any]) -> None:
    """A crashed first attempt may have upserted the point AFTER the later
    delete op already ran; the retry's superseded pre-check must DELETE the
    point, not just skip — otherwise the ghost survives forever."""
    engine: AsyncEngine = api["engine"]
    relay, embedder, bus, server, stub = _pipeline(engine)

    async def scenario() -> tuple[int, int]:
        await _quiesce_outbox(engine)
        record_id = await _write_event(engine, title="ghost", body="ghost point body")
        await embedder.qdrant.ensure_collections()
        async with engine.connect() as conn:
            chunk = (
                await conn.execute(
                    text("SELECT id FROM brain.memory_chunks WHERE record_id = :rid"),
                    {"rid": record_id},
                )
            ).one()
        # The crashed attempt's leftovers: the point exists in Qdrant...
        server.seed_point("titan_memory", str(chunk.id), deterministic_vector("ghost"))
        # ...and a later delete op is already fully processed (its Qdrant delete
        # ran before the point existed, so it no-op'd).
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO brain.ingest_outbox (chunk_id, op, processed_at) "
                    "VALUES (:cid, 'delete', now())"
                ),
                {"cid": chunk.id},
            )
        await relay.relay_once()  # publishes the original upsert op
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        await process_ingest_batch(embedder, bus, "c1", block_ms=1)
        return stub.calls, len(server.points("titan_memory"))

    embed_calls, points = run_async(scenario)
    assert embed_calls == 0  # superseded — never embedded
    assert points == 0  # the ghost point was cleaned up, not skipped


def test_dead_letter_hook_failure_does_not_abort_the_reap_pass() -> None:
    """One failing on_dead hook must not steal the co-claimed deliveries'
    retry (they already paid a delivery count)."""
    import fakeredis.aioredis as fr

    from titan.events.service import Envelope as Env
    from titan.events.service import RedisStreamBus as Bus

    redis = fr.FakeRedis(decode_responses=True)
    bus = Bus(redis)

    async def scenario() -> tuple[int, int, int]:
        topic, group = "t:hook", "g"
        await bus.ensure_group(topic, group)
        await bus.publish(topic, {"n": 1}, actor="t", event_id="poison-1")
        await bus.publish(topic, {"n": 2}, actor="t", event_id="healthy-2")
        # Drive both past max_attempts=1 so BOTH dead-letter in one reap pass.
        await bus.consume(topic, group, "c1", block_ms=1)  # delivered, not acked
        hook_calls = 0

        async def exploding_hook(envelope: Env) -> None:
            nonlocal hook_calls
            hook_calls += 1
            raise RuntimeError("quarantine store is down (test)")

        reclaimed = await bus.reap(
            topic, group, "c2", min_idle_ms=0, max_attempts=1, on_dead=exploding_hook
        )
        dlq_len = int(await redis.xlen("t:hook.dlq"))
        return hook_calls, dlq_len, len(reclaimed)

    hook_calls, dlq_len, reclaimed = run_async(scenario)
    assert hook_calls == 2  # hook attempted for BOTH poison entries
    assert dlq_len == 2  # both still dead-lettered despite the hook failing
    assert reclaimed == 0  # pass completed; nothing stranded in the PEL


def test_quarantine_hook_tolerates_malformed_payloads(api: dict[str, Any]) -> None:
    """A foreign/malformed message on the ingest stream (no numeric outbox_id)
    must be disposed to the DLQ without crashing the quarantine hook."""
    engine: AsyncEngine = api["engine"]
    relay, embedder, bus, _server, _stub = _pipeline(engine)

    async def scenario() -> tuple[int, int]:
        await _quiesce_outbox(engine)
        await bus.ensure_group(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        await bus.publish(
            STREAM_MEMORY_INGEST, {"garbage": True}, actor="foreign", event_id="foreign-1"
        )
        await process_ingest_batch(embedder, bus, "c1", block_ms=1)  # fails, nacks
        for _ in range(MAX_ATTEMPTS + 1):
            await reap_ingest_once(embedder, bus, "c1", min_idle_ms=0)
        redis = bus._redis
        dlq_len = int(await redis.xlen(INGEST_DLQ))
        pending = await bus.pending_depth(STREAM_MEMORY_INGEST, EMBEDDERS_GROUP)
        return dlq_len, pending

    dlq_len, pending = run_async(scenario)
    assert dlq_len == 1  # disposed exactly once, no crash loop
    assert pending == 0


def test_bootstrap_resumes_after_midway_crash() -> None:
    """A bootstrap that crashed after creating the collection but before its
    indexes/alias must converge on retry (indexes re-applied, alias created) —
    not skip indexing forever."""
    from tests.memory_fakes import FakeQdrantServer
    from titan.memory.qdrant import COLLECTION_SPECS, QdrantIndex

    server = FakeQdrantServer()

    async def scenario() -> tuple[int, dict[str, str], dict[str, str]]:
        index = QdrantIndex(server.client())
        spec = COLLECTION_SPECS[0]
        # Simulate the crash: physical collection exists, NO indexes, NO alias.
        server.collections[spec.physical] = {"config": {}, "indexes": {}, "points": {}}
        created = await index.ensure_collections()
        return (
            created,
            dict(server.collections[spec.physical]["indexes"]),
            dict(server.aliases),
        )

    created, indexes, aliases = run_async(scenario)
    assert created == 3  # all three completed (one resumed, two fresh)
    assert indexes == COLLECTION_SPECS[0].payload_indexes  # indexes re-applied
    assert COLLECTION_SPECS[0].alias in aliases  # alias completed


# --- misc units ---------------------------------------------------------------------


def test_backoff_is_exponential_and_capped() -> None:
    assert backoff_seconds(1) == pytest.approx(0.5)
    assert backoff_seconds(2) == pytest.approx(1.0)
    assert backoff_seconds(3) == pytest.approx(2.0)
    assert backoff_seconds(4) == pytest.approx(4.0)
    assert backoff_seconds(10) == pytest.approx(4.0)  # capped


def test_no_provider_sdk_imports_in_memory_service() -> None:
    """TITAN-BRN-006 acceptance: grep shows no direct provider SDK imports in
    the Memory Service — embeddings go only through the gateway."""
    memory_dir = Path(__file__).parent.parent / "src" / "titan" / "memory"
    banned = re.compile(
        r"^\s*(import|from)\s+(anthropic|openai|google|ollama|qdrant_client|voyageai|cohere)\b",
        re.MULTILINE,
    )
    offenders = [
        f"{path.name}: {match.group(0).strip()}"
        for path in sorted(memory_dir.glob("*.py"))
        for match in banned.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_deterministic_vectors_are_stable_units() -> None:
    a1, a2 = deterministic_vector("same text"), deterministic_vector("same text")
    assert a1 == a2 and len(a1) == 768
    norm = sum(x * x for x in a1)
    assert norm == pytest.approx(1.0)
