"""Qdrant over plain HTTP (Vol 5 §4.4, ADR-05-01/05-02) — private to `memory`.

A thin async client on ``httpx`` (no vendor SDK; the same base-URLed client the
/readyz probe uses). Collections are created behind ALIASES: the physical
collection is ``{alias}__v1`` and every read/write goes through the alias, so a
future embedding-model change is a background re-embed into ``__v2`` plus an
atomic alias swap (ADR-05-02) — zero reader disruption.

Point ids equal PostgreSQL ``memory_chunks.id`` (ADR-05-01), which makes
Postgres↔Qdrant reconciliation a set difference (TITAN-BRN-004). All mutating
calls use ``wait=true`` so a success response means the change is durable in
Qdrant — the ingest worker acks its stream delivery only after that.
"""

from dataclasses import dataclass, field
from typing import Any

import httpx

# §4.4: 768-d cosine named vector `dense`; HNSW m=16, ef_construct=128;
# payload on disk. Comfortable laptop-scale defaults (≤5M points, sub-50ms).
VECTOR_NAME = "dense"
VECTOR_SIZE = 768
_HNSW = {"m": 16, "ef_construct": 128}


class QdrantError(RuntimeError):
    """A Qdrant call failed (non-2xx or transport error)."""


@dataclass(frozen=True)
class CollectionSpec:
    """One §4.4 collection: its public alias and its indexed payload fields
    (``field name -> qdrant field schema``)."""

    alias: str
    payload_indexes: dict[str, str] = field(default_factory=dict)

    @property
    def physical(self) -> str:
        # v1 is the bootstrap generation; a re-embed job creates __v2 and swaps
        # the alias (ADR-05-02). Never referenced outside bootstrap/rebuild.
        return f"{self.alias}__v1"


# The §4.4 table, verbatim. UUIDs index as keyword (exact-match filters),
# created_at as datetime (range filters), decay_score as float.
COLLECTION_SPECS: tuple[CollectionSpec, ...] = (
    CollectionSpec(
        alias="titan_memory",
        payload_indexes={
            "scope": "keyword",
            "kind": "keyword",
            "project_id": "keyword",
            "record_id": "keyword",
            "source_agent": "keyword",
            "created_at": "datetime",
            "tags": "keyword",
            "decay_score": "float",
        },
    ),
    CollectionSpec(
        alias="titan_kb",
        payload_indexes={
            "doc_type": "keyword",
            "version": "keyword",
            "scope": "keyword",
            "tags": "keyword",
            "language": "keyword",
        },
    ),
    CollectionSpec(
        alias="titan_kg_labels",
        payload_indexes={"node_type": "keyword", "scope": "keyword"},
    ),
)


class QdrantIndex:
    """The memory module's whole Qdrant surface. ``http`` is base-URLed at the
    Qdrant root (``clients.create_http_client``); pass one backed by a
    ``httpx.MockTransport`` in tests."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    # -- plumbing ------------------------------------------------------------

    async def _call(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise QdrantError(f"qdrant {method} {path}: {exc}") from exc
        if response.status_code >= 400:
            raise QdrantError(f"qdrant {method} {path}: {response.status_code} {response.text}")
        return response.json().get("result")

    # -- bootstrap (§4.4, ADR-05-02) ------------------------------------------

    async def aliases(self) -> dict[str, str]:
        """alias name -> physical collection name."""
        result = await self._call("GET", "/aliases")
        return {a["alias_name"]: a["collection_name"] for a in (result or {}).get("aliases", [])}

    async def collection_exists(self, name: str) -> bool:
        response = await self._http.get(f"/collections/{name}/exists")
        if response.status_code >= 400:
            raise QdrantError(f"qdrant exists {name}: {response.status_code} {response.text}")
        return bool(response.json().get("result", {}).get("exists"))

    async def ensure_collections(self, specs: tuple[CollectionSpec, ...] = COLLECTION_SPECS) -> int:
        """Create every §4.4 collection (behind its alias) that does not already
        exist; idempotent AND resumable: a bootstrap that crashed between create,
        indexing, and aliasing converges on retry (the alias — created LAST — is
        the completion marker). Concurrent workers may race the create; Qdrant
        answers 409 for the loser, which is tolerated when the collection turned
        out to exist. Returns how many collections were newly completed."""
        existing = await self.aliases()
        created = 0
        for spec in specs:
            if spec.alias in existing:
                continue
            if not await self.collection_exists(spec.physical):
                try:
                    await self._call(
                        "PUT",
                        f"/collections/{spec.physical}",
                        json={
                            "vectors": {VECTOR_NAME: {"size": VECTOR_SIZE, "distance": "Cosine"}},
                            "hnsw_config": _HNSW,
                            "on_disk_payload": True,
                        },
                    )
                except QdrantError:
                    # 409 from a concurrent worker's create — fine if it exists
                    # now; anything else is a real failure.
                    if not await self.collection_exists(spec.physical):
                        raise
            # (Re)apply the payload indexes UNCONDITIONALLY — the index PUT is
            # idempotent in Qdrant — so a crash between create and indexing
            # cannot leave a collection permanently unindexed.
            for field_name, schema in spec.payload_indexes.items():
                await self._call(
                    "PUT",
                    f"/collections/{spec.physical}/index?wait=true",
                    json={"field_name": field_name, "field_schema": schema},
                )
            await self._call(
                "POST",
                "/collections/aliases",
                json={
                    "actions": [
                        {
                            "create_alias": {
                                "collection_name": spec.physical,
                                "alias_name": spec.alias,
                            }
                        }
                    ]
                },
            )
            created += 1
        return created

    # -- points ----------------------------------------------------------------

    async def upsert_points(self, collection: str, points: list[dict[str, Any]]) -> None:
        """Idempotent upsert (point id == chunk id); ``wait=true`` so success
        means durable — the caller may ack its delivery."""
        if not points:
            return
        await self._call(
            "PUT", f"/collections/{collection}/points?wait=true", json={"points": points}
        )

    async def delete_points(self, collection: str, ids: list[str]) -> None:
        if not ids:
            return
        await self._call(
            "POST",
            f"/collections/{collection}/points/delete?wait=true",
            json={"points": ids},
        )

    async def list_point_ids(self, collection: str) -> set[str]:
        """Every point id in the collection, via scroll — the Qdrant side of the
        TITAN-BRN-004 set-difference reconciliation."""
        ids: set[str] = set()
        offset: Any = None
        while True:
            body: dict[str, Any] = {"limit": 1000, "with_payload": False, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            result = await self._call("POST", f"/collections/{collection}/points/scroll", json=body)
            for point in result.get("points", []):
                ids.add(str(point["id"]))
            offset = result.get("next_page_offset")
            if offset is None:
                return ids

    async def count(self, collection: str) -> int:
        result = await self._call(
            "POST", f"/collections/{collection}/points/count", json={"exact": True}
        )
        return int(result["count"])

    async def search(
        self,
        collection: str,
        vector: list[float],
        *,
        limit: int = 10,
        query_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Nearest-neighbour search over the named ``dense`` vector. The full §6
        hybrid pipeline lands in Phase 36; this is the primitive it (and the
        rebuild verification) uses."""
        body: dict[str, Any] = {
            "vector": {"name": VECTOR_NAME, "vector": vector},
            "limit": limit,
            "with_payload": True,
        }
        if query_filter is not None:
            body["filter"] = query_filter
        result = await self._call("POST", f"/collections/{collection}/points/search", json=body)
        return list(result)


__all__ = [
    "COLLECTION_SPECS",
    "VECTOR_NAME",
    "VECTOR_SIZE",
    "CollectionSpec",
    "QdrantError",
    "QdrantIndex",
]
