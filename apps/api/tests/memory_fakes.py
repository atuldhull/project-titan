"""Test doubles for the Phase-35 embedding pipeline.

``FakeQdrantServer`` is an in-memory Qdrant REST double served through
``httpx.MockTransport`` — the same wire the real ``QdrantIndex`` speaks, so the
client code under test is byte-identical to production. ``StubEmbeddings`` is a
deterministic 768-d embedder satisfying the ``EmbeddingsClient`` protocol
(embedding the same text always yields the same unit vector, so search-for-self
returns cosine ≈ 1.0).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from decimal import Decimal
from typing import Any

import httpx

from titan.llm.service import EmbeddingsRequest, EmbeddingsResponse, EmbeddingVector, Usage
from titan.memory.qdrant import VECTOR_NAME, VECTOR_SIZE

_POINTS_RE = re.compile(
    r"^/collections/(?P<name>[^/]+)/points(?:/(?P<action>scroll|delete|count|search))?$"
)


def deterministic_vector(text: str, dim: int = VECTOR_SIZE) -> list[float]:
    """A unit vector derived only from ``text`` — stable across processes."""
    rng = random.Random(hashlib.sha256(text.encode("utf-8")).hexdigest())
    raw = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


class StubEmbeddings:
    """Deterministic EmbeddingsClient. ``fail_times`` makes the next N calls
    raise — the poison/retry path in tests."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    async def embed(self, req: EmbeddingsRequest) -> EmbeddingsResponse:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("stub embeddings failure (test-injected)")
        vectors = [
            EmbeddingVector(index=i, embedding=deterministic_vector(text))
            for i, text in enumerate(req.input)
        ]
        return EmbeddingsResponse(
            embeddings=vectors,
            dimensions=VECTOR_SIZE,
            provider="stub",
            model_id="stub-embed-1",
            usage=Usage(input_tokens=sum(len(t.split()) for t in req.input)),
            cost_usd=Decimal("0"),
            latency_ms=1,
        )


class FakeQdrantServer:
    """The subset of the Qdrant REST API the memory module uses, in memory."""

    def __init__(self) -> None:
        # physical name -> {"config": .., "indexes": {field: schema}, "points": {id: {...}}}
        self.collections: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        self.upsert_calls = 0
        self.delete_calls = 0
        self.fail_upserts = 0  # next N upserts return 503 (transient-failure path)
        self.search_calls: list[str] = []  # collection names, in call order

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle), base_url="http://qdrant.test"
        )

    # -- convenience for assertions -------------------------------------------

    def points(self, name: str) -> dict[str, dict[str, Any]]:
        resolved = self.aliases.get(name, name)
        collection = self.collections.get(resolved)
        return dict(collection["points"]) if collection else {}

    def seed_point(self, name: str, point_id: str, vector: list[float]) -> None:
        resolved = self.aliases.get(name, name)
        self.collections[resolved]["points"][point_id] = {
            "vector": {VECTOR_NAME: vector},
            "payload": {},
        }

    # -- the wire ---------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        path, method = request.url.path, request.method
        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content)

        if method == "GET" and path == "/aliases":
            aliases = [
                {"alias_name": alias, "collection_name": name}
                for alias, name in self.aliases.items()
            ]
            return self._ok({"aliases": aliases})
        if method == "GET" and path.endswith("/exists") and path.startswith("/collections/"):
            name = path.split("/")[2]
            return self._ok({"exists": name in self.collections})
        if method == "PUT" and re.fullmatch(r"/collections/[^/]+", path):
            name = path.split("/")[2]
            self.collections[name] = {"config": body, "indexes": {}, "points": {}}
            return self._ok(True)
        if method == "PUT" and re.fullmatch(r"/collections/[^/]+/index", path):
            name = path.split("/")[2]
            self.collections[name]["indexes"][body["field_name"]] = body["field_schema"]
            return self._ok(True)
        if method == "POST" and path == "/collections/aliases":
            for action in body.get("actions", []):
                create = action.get("create_alias")
                if create:
                    self.aliases[create["alias_name"]] = create["collection_name"]
            return self._ok(True)

        match = _POINTS_RE.fullmatch(path)
        if match is None:
            return httpx.Response(404, json={"status": {"error": f"no route {path}"}})
        name = self.aliases.get(match["name"], match["name"])
        if name not in self.collections:
            return httpx.Response(404, json={"status": {"error": f"collection {name}"}})
        store: dict[str, dict[str, Any]] = self.collections[name]["points"]
        action = match["action"]

        if method == "PUT" and action is None:  # upsert
            if self.fail_upserts > 0:
                self.fail_upserts -= 1
                return httpx.Response(503, json={"status": {"error": "unavailable (test)"}})
            self.upsert_calls += 1
            for point in body["points"]:
                store[str(point["id"])] = {
                    "vector": point["vector"],
                    "payload": point.get("payload", {}),
                }
            return self._ok({"status": "completed"})
        if method == "POST" and action == "delete":
            self.delete_calls += 1
            for point_id in body["points"]:
                store.pop(str(point_id), None)
            return self._ok({"status": "completed"})
        if method == "POST" and action == "count":
            return self._ok({"count": len(store)})
        if method == "POST" and action == "scroll":
            ordered = sorted(store)
            start = 0
            if body.get("offset") is not None:
                start = ordered.index(str(body["offset"]))
            limit = int(body.get("limit", 10))
            page = ordered[start : start + limit]
            next_offset = ordered[start + limit] if start + limit < len(ordered) else None
            return self._ok(
                {
                    "points": [{"id": point_id} for point_id in page],
                    "next_page_offset": next_offset,
                }
            )
        if method == "POST" and action == "search":
            self.search_calls.append(match["name"])  # pre-alias-resolution name
            query = body["vector"]["vector"]
            conditions = (body.get("filter") or {}).get("must", [])
            scored = []
            for point_id, point in store.items():
                if not _matches(point["payload"], conditions):
                    continue
                vector = point["vector"][VECTOR_NAME]
                score = _cosine(query, vector)
                scored.append({"id": point_id, "score": score, "payload": point["payload"]})
            scored.sort(key=lambda item: item["score"], reverse=True)
            return self._ok(scored[: int(body.get("limit", 10))])
        return httpx.Response(404, json={"status": {"error": f"no route {method} {path}"}})

    @staticmethod
    def _ok(result: Any) -> httpx.Response:
        return httpx.Response(200, json={"result": result, "status": "ok"})


def _matches(payload: dict[str, Any], must: list[dict[str, Any]]) -> bool:
    """Qdrant `must` filter subset: {"key", "match": {"value": x} | {"any": [..]}}.
    A list-valued payload field (tags) matches when it CONTAINS the target."""
    for condition in must:
        value = payload.get(condition["key"])
        match = condition.get("match", {})
        if "value" in match:
            wanted = [match["value"]]
        elif "any" in match:
            wanted = list(match["any"])
        else:
            return False
        if isinstance(value, list):
            if not any(v in wanted for v in value):
                return False
        elif value not in wanted:
            return False
    return True


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (norm_a * norm_b)
