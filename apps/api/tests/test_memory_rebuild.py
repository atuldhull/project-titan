"""rebuild-index (TITAN-BRN-004): set-difference reconciliation from PostgreSQL,
the admin endpoint's RBAC + wiring, and the `titan` CLI parser."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from tests.memory_fakes import FakeQdrantServer, StubEmbeddings, deterministic_vector
from titan.cli import build_parser
from titan.memory import service as memory
from titan.memory.qdrant import QdrantIndex
from titan.memory.service import MemoryEvent, MemoryEventBatch

pytestmark = pytest.mark.schema


async def _write_events(engine: AsyncEngine, count: int, *, prefix: str) -> list[uuid.UUID]:
    batch = MemoryEventBatch(
        source_agent="memory_agent",
        events=[
            MemoryEvent(
                kind="semantic",
                scope="org",
                title=f"{prefix}-{i}",
                body_md=f"{prefix} body number {i} with distinct content",
            )
            for i in range(count)
        ],
    )
    async with engine.begin() as conn:
        result = await memory.write_events(
            conn, batch, source_agent="memory_agent", actor_agent="test"
        )
    return [r.record_id for r in result.records]


def test_rebuild_reconciles_by_point_id_set_difference(api: dict[str, Any]) -> None:
    """Strays (in Qdrant, not in PG) deleted; missing (in PG, not in Qdrant)
    embedded + upserted; chunks restamped."""
    engine: AsyncEngine = api["engine"]
    server = FakeQdrantServer()
    stub = StubEmbeddings()

    async def scenario() -> tuple[Any, int, set[str], int]:
        record_ids = await _write_events(engine, 3, prefix="rebuild")
        index = QdrantIndex(server.client())
        await index.ensure_collections()
        # A stray point that has no PostgreSQL chunk behind it:
        stray = str(uuid.uuid4())
        server.seed_point("titan_memory", stray, deterministic_vector("stray"))
        report = await memory.rebuild_index(engine, index, stub)
        async with engine.connect() as conn:
            # The titan_memory canonical set is ACTIVE, NON-KB records' chunks
            # (Phase 37 + Phase 39 partition): other tests on the shared DB may
            # leave superseded residue (rebuild treats it as strays) OR active KB
            # records (whose chunks belong to titan_kb, not titan_memory) — both
            # must be excluded from this titan_memory equality.
            pg_ids = {
                str(row.id)
                for row in (
                    await conn.execute(
                        text(
                            "SELECT c.id FROM brain.memory_chunks c "
                            "JOIN brain.memory_records r ON r.id = c.record_id "
                            "WHERE r.status = 'active' AND NOT (r.metadata ? 'kb_slug')"
                        )
                    )
                ).all()
            }
            unstamped = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.memory_chunks c "
                        "JOIN brain.memory_records r ON r.id = c.record_id "
                        "WHERE c.embedded_at IS NULL AND r.id = ANY(CAST(:rids AS uuid[]))"
                    ),
                    {"rids": record_ids},
                )
            ).scalar_one()
        return report, len(pg_ids), set(server.points("titan_memory")), int(unstamped)

    report, pg_count, qdrant_ids, unstamped = run_async(scenario)
    stats = report.collections["titan_memory"]
    assert stats.strays_deleted >= 1  # the seeded stray is gone
    assert stats.chunks_embedded >= 3  # this test's three records re-embedded
    assert stats.points == len(qdrant_ids)  # report matches the actual index
    # Canonical set = active chunks MINUS dead-lettered poison (rebuild skips
    # quarantine): another test's live dead_at outbox row on the shared DB
    # must not break the equality.
    assert stats.points == pg_count - stats.quarantined
    assert unstamped == 0  # embedded_at restamped (TITAN-BRN-004)


def test_rebuild_is_idempotent_second_run_is_a_no_op(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    server = FakeQdrantServer()
    stub = StubEmbeddings()

    async def scenario() -> tuple[Any, Any]:
        await _write_events(engine, 2, prefix="idem")
        index = QdrantIndex(server.client())
        first = await memory.rebuild_index(engine, index, stub)
        second = await memory.rebuild_index(engine, index, stub)
        return first, second

    first, second = run_async(scenario)
    assert second.collections["titan_memory"].chunks_embedded == 0
    assert second.collections["titan_memory"].strays_deleted == 0
    assert second.collections["titan_memory"].points == first.collections["titan_memory"].points


def test_rebuild_purges_derived_only_collections(api: dict[str, Any]) -> None:
    """titan_kg_labels has no canonical table (entity resolution is out of MVP
    scope), so rebuild reconciles it to empty. titan_kb IS reconciled from
    PostgreSQL now (Phase 39): a stray with no backing KB chunk is still a stray
    to delete, keeping 'derivable from PostgreSQL' true for EVERY collection."""
    engine: AsyncEngine = api["engine"]
    server = FakeQdrantServer()
    kb_stray = str(uuid.uuid4())

    async def scenario() -> Any:
        index = QdrantIndex(server.client())
        await index.ensure_collections()
        server.seed_point("titan_kb", kb_stray, deterministic_vector("kb-stray"))
        server.seed_point("titan_kg_labels", str(uuid.uuid4()), deterministic_vector("kg-stray"))
        report = await memory.rebuild_index(engine, index, StubEmbeddings())
        kb_ids = await index.list_point_ids("titan_kb")
        return report, kb_ids

    report, kb_ids = run_async(scenario)
    # The unbacked titan_kb point is deleted as a stray (exact count is tolerant
    # of KB records other tests may have left on the shared session DB).
    assert kb_stray not in kb_ids
    assert report.collections["titan_kb"].strays_deleted >= 1
    assert report.collections["titan_kg_labels"].strays_deleted == 1
    assert report.collections["titan_kg_labels"].points == 0


def test_rebuild_bootstraps_collections_on_a_bare_qdrant(api: dict[str, Any]) -> None:
    """The volume-deletion scenario: an empty Qdrant gets its collections
    recreated and repopulated in one command (TITAN-BRN-004)."""
    engine: AsyncEngine = api["engine"]
    server = FakeQdrantServer()  # bare: no collections at all

    async def scenario() -> Any:
        return await memory.rebuild_index(engine, QdrantIndex(server.client()), StubEmbeddings())

    report = run_async(scenario)
    assert report.collections_created == 3
    assert "titan_memory" in server.aliases
    assert report.collections["titan_memory"].points >= 0  # populated from PG


def test_live_rebuild_never_deletes_a_concurrently_ingested_point(api: dict[str, Any]) -> None:
    """Live-rebuild safety (snapshot ordering): a chunk written+embedded by the
    running pipeline WHILE rebuild is snapshotting must never be misclassified
    as a stray and deleted. The scroll runs BEFORE the PG read, so any point in
    the scroll has its chunk in the (later) PG snapshot. With the reverse order
    this test deletes the fresh point — permanently unsearchable."""
    engine: AsyncEngine = api["engine"]
    server = FakeQdrantServer()

    class MidScrollIngestIndex(QdrantIndex):
        """Simulates a concurrent embedder finishing between rebuild's two
        snapshots: a new chunk commits and its point lands JUST BEFORE the
        scroll observes the collection."""

        def __init__(self) -> None:
            super().__init__(server.client())
            self.fresh_chunk_id: str | None = None

        async def list_point_ids(self, collection: str) -> set[str]:
            if collection == "titan_memory" and self.fresh_chunk_id is None:
                record_id = (await _write_events(engine, 1, prefix="mid-rebuild"))[0]
                async with engine.connect() as conn:
                    chunk = (
                        await conn.execute(
                            text("SELECT id FROM brain.memory_chunks WHERE record_id = :rid"),
                            {"rid": record_id},
                        )
                    ).first()
                assert chunk is not None
                self.fresh_chunk_id = str(chunk.id)
                server.seed_point(
                    "titan_memory", self.fresh_chunk_id, deterministic_vector("fresh")
                )
            return await super().list_point_ids(collection)

    async def scenario() -> tuple[str | None, set[str]]:
        index = MidScrollIngestIndex()
        await index.ensure_collections()
        await memory.rebuild_index(engine, index, StubEmbeddings())
        return index.fresh_chunk_id, set(server.points("titan_memory"))

    fresh_chunk_id, points = run_async(scenario)
    assert fresh_chunk_id is not None
    assert fresh_chunk_id in points  # the concurrently-ingested point SURVIVES


def test_rebuild_skips_quarantined_poison_and_still_completes(api: dict[str, Any]) -> None:
    """A dead-lettered (poison) chunk must not abort the TITAN-BRN-004 recovery
    command: it is skipped (reported as quarantined) and everything else is
    rebuilt."""
    engine: AsyncEngine = api["engine"]
    server = FakeQdrantServer()

    async def scenario() -> Any:
        healthy = await _write_events(engine, 2, prefix="healthy")
        poison = await _write_events(engine, 1, prefix="poison")
        # Quarantine the poison record's outbox rows (what the reaper's
        # dead-letter hook does after MAX_ATTEMPTS failed deliveries).
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE brain.ingest_outbox SET dead_at = now() WHERE chunk_id IN "
                    "(SELECT id FROM brain.memory_chunks WHERE record_id = :rid)"
                ),
                {"rid": poison[0]},
            )
        report = await memory.rebuild_index(engine, QdrantIndex(server.client()), StubEmbeddings())
        async with engine.connect() as conn:
            healthy_embedded = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM brain.memory_chunks c "
                        "JOIN brain.memory_records r ON r.id = c.record_id "
                        "WHERE r.id = ANY(CAST(:ids AS uuid[])) AND c.embedded_at IS NOT NULL"
                    ),
                    {"ids": [str(i) for i in healthy]},
                )
            ).scalar_one()
        return report, int(healthy_embedded)

    report, healthy_embedded = run_async(scenario)
    stats = report.collections["titan_memory"]
    assert stats.quarantined >= 1  # the poison chunk was skipped, not retried
    assert healthy_embedded >= 2  # the healthy records were rebuilt
    assert stats.failed == 0


def test_rebuild_isolates_a_failing_chunk_and_finishes(api: dict[str, Any]) -> None:
    """An embed failure mid-rebuild must not sink the batch or the command:
    the failing chunk is counted in `failed`, its batch-mates still embed."""
    engine: AsyncEngine = api["engine"]
    server = FakeQdrantServer()

    class FlakyEmbeddings(StubEmbeddings):
        """Fails the first (batched) call, then fails exactly one single-chunk
        retry — deterministic poison for one chunk, success for the rest."""

        def __init__(self) -> None:
            super().__init__()
            self.batch_failures = 0
            self.single_failures = 0

        async def embed(self, req: Any) -> Any:
            if len(req.input) > 1 and self.batch_failures == 0:
                self.batch_failures += 1
                raise RuntimeError("batch embed failure (test)")
            if len(req.input) == 1 and self.single_failures == 0:
                self.single_failures += 1
                raise RuntimeError("single embed failure (test)")
            return await super().embed(req)

    async def scenario() -> Any:
        await _write_events(engine, 3, prefix="isolate")
        return await memory.rebuild_index(engine, QdrantIndex(server.client()), FlakyEmbeddings())

    report = run_async(scenario)
    stats = report.collections["titan_memory"]
    assert stats.failed == 1  # exactly the one deterministic loser
    assert stats.chunks_embedded >= 2  # its batch-mates were rescued
    assert stats.points == stats.chunks_embedded  # report is consistent


# --- admin endpoint ------------------------------------------------------------


def test_admin_rebuild_endpoint_requires_memory_admin(api: dict[str, Any]) -> None:
    server = FakeQdrantServer()
    app = api["client"].app
    original_http, original_gateway = app.state.http, app.state.llm_gateway
    app.state.http = server.client()  # swap the Qdrant client for the fake
    app.state.llm_gateway = StubEmbeddings()
    try:
        denied = api["client"].post(
            "/api/v1/memory/admin/rebuild-index", headers=api["token_for"]("operator")
        )
        assert denied.status_code == 403  # operator lacks memory:admin

        allowed = api["client"].post(
            "/api/v1/memory/admin/rebuild-index", headers=api["token_for"]("admin")
        )
        assert allowed.status_code == 200, allowed.text
        body = allowed.json()
        assert set(body["collections"]) == {"titan_memory", "titan_kb", "titan_kg_labels"}
    finally:
        # Restore so app shutdown acloses the real clients, not the fakes.
        app.state.http, app.state.llm_gateway = original_http, original_gateway


def test_admin_rebuild_rejects_anonymous(api: dict[str, Any]) -> None:
    response = api["client"].post("/api/v1/memory/admin/rebuild-index")
    assert response.status_code == 401


# --- CLI -------------------------------------------------------------------------


def test_cli_parser_accepts_memory_rebuild_index() -> None:
    args = build_parser().parse_args(["memory", "rebuild-index"])
    assert args.command == "memory" and args.memory_command == "rebuild-index"


def test_cli_parser_rejects_unknown_commands() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["memory", "frobnicate"])
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
