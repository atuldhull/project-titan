"""QdrantIndex + §4.4 collection bootstrap (Vol 5, ADR-05-01/05-02) — pure unit
tests over the REST wire via httpx.MockTransport (no infra)."""

from __future__ import annotations

import pytest

from tests.conftest import run_async
from tests.memory_fakes import FakeQdrantServer, deterministic_vector
from titan.memory.qdrant import (
    COLLECTION_SPECS,
    VECTOR_NAME,
    VECTOR_SIZE,
    QdrantError,
    QdrantIndex,
)


def test_bootstrap_creates_all_4_4_collections_behind_aliases() -> None:
    server = FakeQdrantServer()

    async def scenario() -> int:
        return await QdrantIndex(server.client()).ensure_collections()

    assert run_async(scenario) == 3
    # Aliased physical collections (ADR-05-02: re-embed → new physical + swap).
    assert server.aliases == {
        "titan_memory": "titan_memory__v1",
        "titan_kb": "titan_kb__v1",
        "titan_kg_labels": "titan_kg_labels__v1",
    }
    for spec in COLLECTION_SPECS:
        config = server.collections[spec.physical]["config"]
        # §4.4: 768-d cosine named vector `dense`; HNSW m=16/ef_construct=128;
        # payload on disk.
        assert config["vectors"] == {VECTOR_NAME: {"size": VECTOR_SIZE, "distance": "Cosine"}}
        assert config["hnsw_config"] == {"m": 16, "ef_construct": 128}
        assert config["on_disk_payload"] is True
        assert server.collections[spec.physical]["indexes"] == spec.payload_indexes


def test_bootstrap_is_idempotent() -> None:
    server = FakeQdrantServer()

    async def scenario() -> tuple[int, int]:
        index = QdrantIndex(server.client())
        first = await index.ensure_collections()
        second = await index.ensure_collections()  # aliases exist → nothing to do
        return first, second

    assert run_async(scenario) == (3, 0)


def test_titan_memory_payload_indexes_match_the_4_4_table() -> None:
    spec = next(s for s in COLLECTION_SPECS if s.alias == "titan_memory")
    assert set(spec.payload_indexes) == {
        "scope",
        "kind",
        "project_id",
        "record_id",
        "source_agent",
        "created_at",
        "tags",
        "decay_score",
    }
    kb = next(s for s in COLLECTION_SPECS if s.alias == "titan_kb")
    assert set(kb.payload_indexes) == {"doc_type", "version", "scope", "tags", "language"}
    kg = next(s for s in COLLECTION_SPECS if s.alias == "titan_kg_labels")
    assert set(kg.payload_indexes) == {"node_type", "scope"}


def test_point_ops_roundtrip_through_the_alias() -> None:
    server = FakeQdrantServer()
    point_id = "11111111-1111-1111-1111-111111111111"
    vector = deterministic_vector("hello world")

    async def scenario() -> tuple[int, set[str], list[dict[str, object]], int]:
        index = QdrantIndex(server.client())
        await index.ensure_collections()
        await index.upsert_points(
            "titan_memory", [{"id": point_id, "vector": {VECTOR_NAME: vector}}]
        )
        count = await index.count("titan_memory")
        ids = await index.list_point_ids("titan_memory")
        hits = await index.search("titan_memory", vector, limit=1)
        await index.delete_points("titan_memory", [point_id])
        return count, ids, hits, await index.count("titan_memory")

    count, ids, hits, after_delete = run_async(scenario)
    assert count == 1 and ids == {point_id} and after_delete == 0
    assert hits and hits[0]["id"] == point_id
    assert hits[0]["score"] == pytest.approx(1.0)


def test_scroll_paginates_beyond_one_page() -> None:
    server = FakeQdrantServer()
    ids = {f"00000000-0000-0000-0000-{i:012d}" for i in range(2500)}  # > 2 scroll pages

    async def scenario() -> set[str]:
        index = QdrantIndex(server.client())
        await index.ensure_collections()
        for point_id in ids:
            server.seed_point("titan_memory", point_id, deterministic_vector(point_id))
        return await index.list_point_ids("titan_memory")

    assert run_async(scenario) == ids


def test_non_2xx_raises_qdrant_error() -> None:
    server = FakeQdrantServer()

    async def scenario() -> None:
        index = QdrantIndex(server.client())
        await index.ensure_collections()
        server.fail_upserts = 1
        await index.upsert_points(
            "titan_memory", [{"id": "x", "vector": {VECTOR_NAME: deterministic_vector("x")}}]
        )

    with pytest.raises(QdrantError):
        run_async(scenario)


def test_empty_batches_are_no_ops() -> None:
    server = FakeQdrantServer()

    async def scenario() -> None:
        index = QdrantIndex(server.client())
        await index.ensure_collections()
        await index.upsert_points("titan_memory", [])
        await index.delete_points("titan_memory", [])

    run_async(scenario)
    assert server.upsert_calls == 0 and server.delete_calls == 0
