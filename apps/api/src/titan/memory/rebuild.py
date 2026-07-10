"""`rebuild-index` (TITAN-BRN-004): reconstruct every Qdrant collection from
PostgreSQL — the recovery path that makes Qdrant safely disposable (ADR-05-01).

Reconciliation is a set difference on point-id == chunk-id, where the
canonical set is chunks of ACTIVE records only (the live pipeline deletes a
record's points on supersede, and §9.2 archiving drops vectors — a rebuild
must converge to the same state, so non-active records' leftover points are
strays):

* points in Qdrant with no ACTIVE PostgreSQL chunk → deleted (strays);
* active chunks with no Qdrant point → re-embedded through the gateway
  (TITAN-BRN-006) and upserted, with ``embedded_at``/``embed_model`` restamped.

``titan_kb`` and ``titan_kg_labels`` have no canonical source tables until
their volumes' phases (§10 KB, §7 knowledge graph), so their canonical set is
empty today: rebuild reconciles them to empty (strays deleted), keeping the
invariant that EVERY collection is fully derivable from PostgreSQL.
"""

import uuid
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.llm.service import EmbeddingsRequest, RequestMetadata
from titan.memory import repository as repo
from titan.memory.ingest import build_point
from titan.memory.qdrant import QdrantIndex

if TYPE_CHECKING:
    from titan.memory.ingest import EmbeddingsClient

logger = structlog.get_logger("titan.memory")

MEMORY_COLLECTION = "titan_memory"
_DERIVED_ONLY_COLLECTIONS = ("titan_kb", "titan_kg_labels")
_DELETE_BATCH = 500


class CollectionRebuild(BaseModel):
    """What reconciliation did to one collection. ``quarantined`` chunks were
    skipped (dead-lettered poison awaiting operator replay); ``failed`` chunks
    hit an embed/upsert error during THIS rebuild and were skipped so the
    recovery command still completes (TITAN-BRN-004)."""

    strays_deleted: int
    chunks_embedded: int
    points: int
    quarantined: int = 0
    failed: int = 0


class RebuildReport(BaseModel):
    """The TITAN-BRN-004 rebuild summary, per collection alias."""

    collections_created: int
    collections: dict[str, CollectionRebuild]


async def _embed_and_upsert(
    engine: AsyncEngine,
    qdrant: QdrantIndex,
    embeddings: "EmbeddingsClient",
    rows: list[Any],
) -> int:
    """Embed one batch of chunk rows through the gateway, upsert the points,
    restamp the chunks. Returns how many were embedded."""
    response = await embeddings.embed(
        EmbeddingsRequest(
            input=[row.text for row in rows],
            metadata=RequestMetadata(agent_id="memory.rebuild"),
        )
    )
    points = [build_point(row, response.embeddings[i].embedding) for i, row in enumerate(rows)]
    await qdrant.upsert_points(MEMORY_COLLECTION, points)
    async with engine.begin() as conn:
        await repo.mark_chunks_embedded(
            conn, [row.chunk_id for row in rows], embed_model=response.model_id
        )
    return len(rows)


async def rebuild_index(
    engine: AsyncEngine,
    qdrant: QdrantIndex,
    embeddings: "EmbeddingsClient",
    *,
    embed_batch: int = 16,
) -> RebuildReport:
    """Fully reconstruct all §4.4 collections from PostgreSQL (TITAN-BRN-004).
    Idempotent and safe on a live system: existing correct points are left in
    place (upsert-by-id), so a no-op rebuild is cheap."""
    created = await qdrant.ensure_collections()
    report: dict[str, CollectionRebuild] = {}

    # -- titan_memory: two-way set difference against brain.memory_chunks -----
    # SNAPSHOT ORDER MATTERS on a live system: Qdrant FIRST, PostgreSQL second.
    # A point cannot exist before its chunk row committed, so every point in the
    # earlier Qdrant snapshot that is missing from the LATER PG snapshot is a
    # genuine stray — a chunk ingested mid-rebuild lands in the PG snapshot (or
    # in neither) and is never misclassified and deleted. The reverse order
    # would silently delete freshly-ingested points whose outbox rows are
    # already processed, leaving them unsearchable until the next rebuild.
    # (When delete ops ship — prune, §9 — rebuild must additionally quiesce or
    # timestamp-guard; today no producer emits deletes.)
    qdrant_ids = await qdrant.list_point_ids(MEMORY_COLLECTION)
    async with engine.connect() as conn:
        pg_ids = {str(chunk_id) for chunk_id in await repo.all_chunk_ids(conn)}
        quarantined_ids = {str(cid) for cid in await repo.quarantined_chunk_ids(conn)}
    strays = sorted(qdrant_ids - pg_ids)
    # Dead-lettered poison is SKIPPED, not retried: a deterministic embed
    # failure would otherwise abort the recovery command itself. An operator
    # replay clears the quarantine and re-runs rebuild.
    missing = sorted(pg_ids - qdrant_ids - quarantined_ids)
    quarantined = len((pg_ids & quarantined_ids) - qdrant_ids)
    for start in range(0, len(strays), _DELETE_BATCH):
        await qdrant.delete_points(MEMORY_COLLECTION, strays[start : start + _DELETE_BATCH])
    embedded = 0
    failed = 0
    for start in range(0, len(missing), embed_batch):
        batch_ids = [uuid.UUID(point_id) for point_id in missing[start : start + embed_batch]]
        async with engine.connect() as conn:
            rows = await repo.chunks_for_rebuild(conn, batch_ids)
        if not rows:
            continue
        try:
            embedded += await _embed_and_upsert(engine, qdrant, embeddings, list(rows))
        except Exception:
            # One bad chunk must not sink its 15 batch-mates or the whole
            # rebuild: retry the batch chunk-by-chunk, counting the losers.
            logger.warning("memory.rebuild_batch_failed", batch_start=start, size=len(rows))
            for row in rows:
                try:
                    embedded += await _embed_and_upsert(engine, qdrant, embeddings, [row])
                except Exception:
                    failed += 1
                    logger.exception("memory.rebuild_chunk_failed", chunk_id=str(row.chunk_id))
    report[MEMORY_COLLECTION] = CollectionRebuild(
        strays_deleted=len(strays),
        chunks_embedded=embedded,
        points=await qdrant.count(MEMORY_COLLECTION),
        quarantined=quarantined,
        failed=failed,
    )

    # -- derived-only collections: canonical set is empty until their phases --
    for alias in _DERIVED_ONLY_COLLECTIONS:
        ids = sorted(await qdrant.list_point_ids(alias))
        for start in range(0, len(ids), _DELETE_BATCH):
            await qdrant.delete_points(alias, ids[start : start + _DELETE_BATCH])
        report[alias] = CollectionRebuild(
            strays_deleted=len(ids), chunks_embedded=0, points=await qdrant.count(alias)
        )

    logger.info(
        "memory.rebuild_index",
        collections_created=created,
        **{alias: stats.model_dump() for alias, stats in report.items()},
    )
    return RebuildReport(collections_created=created, collections=report)


__all__ = ["CollectionRebuild", "MEMORY_COLLECTION", "RebuildReport", "rebuild_index"]
