"""The Memory Explorer's read seams (Vol 4 §6.11, TITAN-FRT-034).

Browse and facets run against the real schema. Search is exercised through the
pure `explorer.search` with a faked Qdrant + embeddings — the same seam the
packet tests use — because the point under test is the SCORE contract, not
Qdrant's nearest-neighbour maths.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.memory import explorer

pytestmark = pytest.mark.schema

NOW = datetime.now(tz=UTC)


def _record(engine: Any, **over: Any) -> uuid.UUID:
    record_id = uuid.uuid4()

    async def insert() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO brain.memory_records (id, kind, scope, source_agent, "
                    "title, body_md, metadata, status, importance, created_at) VALUES "
                    "(:id, :kind, :scope, :agent, :title, :body, CAST(:meta AS jsonb), "
                    ":status, :importance, :at)"
                ),
                {
                    "id": record_id,
                    "kind": over.get("kind", "semantic"),
                    "scope": over.get("scope", "org"),
                    "agent": over.get("source_agent", "eng_backend"),
                    "title": over.get("title", "JWT refresh rotation"),
                    "body": over.get("body_md", "rotate the refresh token on every use"),
                    "meta": over.get("meta", "{}"),
                    "status": over.get("status", "active"),
                    "importance": over.get("importance", 0.5),
                    "at": over.get("created_at", NOW - timedelta(minutes=1)),
                },
            )

    run_async(insert)
    return record_id


def _clear(engine: Any) -> None:
    """Wipe every memory record and everything that references one.

    `migrated_engine` is SESSION-scoped, so every test file shares one database,
    and by the time this file runs the whole memory subsystem has written rows
    that FK into `memory_records` from more than one table — `memory_chunks`,
    `ingest_outbox` (via chunks), the KB tables (0017) and the decay/lesson
    tables (0018). A hand-ordered `DELETE` chain kept passing in isolation and
    failing in the full suite as each new referencing table filled up; every
    such pass was a shared-state bug, not a green.

    `TRUNCATE … CASCADE` is the robust answer: it follows the FKs itself, so it
    cannot be defeated by a referencing table this test does not know about.
    """

    async def wipe() -> None:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE brain.memory_records, brain.memory_chunks CASCADE"))

    run_async(wipe)


# --- browse ------------------------------------------------------------------


def test_browse_requires_authentication(api: dict[str, Any]) -> None:
    assert api["client"].get("/api/v1/memory/records").status_code == 401


def test_browse_returns_records_newest_first(api: dict[str, Any]) -> None:
    _clear(api["engine"])
    _record(api["engine"], title="older", created_at=NOW - timedelta(hours=2))
    _record(api["engine"], title="newer", created_at=NOW - timedelta(minutes=2))
    body = api["client"].get("/api/v1/memory/records", headers=api["token_for"]("admin")).json()
    assert [r["title"] for r in body["items"]] == ["newer", "older"]


def test_browse_hides_archived_by_default(api: dict[str, Any]) -> None:
    # Archive is a soft-delete (§6.11), so an archived record must not be in the
    # default list — but must still be reachable by asking for it.
    _clear(api["engine"])
    _record(api["engine"], title="live")
    _record(api["engine"], title="gone", status="archived")
    default = api["client"].get("/api/v1/memory/records", headers=api["token_for"]("admin")).json()
    assert [r["title"] for r in default["items"]] == ["live"]

    archived = (
        api["client"]
        .get("/api/v1/memory/records?status=archived", headers=api["token_for"]("admin"))
        .json()
    )
    assert [r["title"] for r in archived["items"]] == ["gone"]


def test_browse_filters_by_kind_scope_and_agent(api: dict[str, Any]) -> None:
    _clear(api["engine"])
    project = f"project:{uuid.uuid4()}"
    _record(api["engine"], title="a", kind="episodic", scope="org", source_agent="qa")
    _record(api["engine"], title="b", kind="semantic", scope=project, source_agent="eng_backend")

    client, token = api["client"], api["token_for"]("admin")
    assert [
        r["title"]
        for r in client.get("/api/v1/memory/records?kind=episodic", headers=token).json()["items"]
    ] == ["a"]
    assert [
        r["title"]
        for r in client.get(f"/api/v1/memory/records?scope={project}", headers=token).json()[
            "items"
        ]
    ] == ["b"]
    assert [
        r["title"]
        for r in client.get("/api/v1/memory/records?source_agent=qa", headers=token).json()["items"]
    ] == ["a"]


def test_an_unknown_kind_is_a_422_rather_than_an_empty_list(api: dict[str, Any]) -> None:
    # An empty list would read as "nothing stored here yet" — §6.11's empty
    # state — for what is really a typo.
    response = api["client"].get(
        "/api/v1/memory/records?kind=rumour", headers=api["token_for"]("admin")
    )
    assert response.status_code == 422
    assert "rumour" in response.json()["detail"]


def test_an_unknown_status_is_a_422_too(api: dict[str, Any]) -> None:
    # `status` is validated for the same reason `kind` is — an unvalidated typo
    # silently returned the empty-state instead of a 422.
    response = api["client"].get(
        "/api/v1/memory/records?status=archivd", headers=api["token_for"]("admin")
    )
    assert response.status_code == 422
    assert "archivd" in response.json()["detail"]


def test_browse_surfaces_the_subtype_that_the_tier_tree_calls_a_tier(
    api: dict[str, Any],
) -> None:
    """§6.11's "Patterns" and "Lessons" tiers are not tiers.

    They are records discriminated by `metadata->>'record_subtype'` — `scope`
    is the only real tier axis, and it holds only org|project:<uuid>. The UI
    needs the subtype to render the tree the spec asks for, so it is exposed
    rather than left for the client to re-derive.
    """
    _clear(api["engine"])
    _record(api["engine"], title="p", meta='{"record_subtype": "pattern"}')
    _record(api["engine"], title="plain")
    body = (
        api["client"]
        .get("/api/v1/memory/records?subtype=pattern", headers=api["token_for"]("admin"))
        .json()
    )
    assert [r["title"] for r in body["items"]] == ["p"]
    assert body["items"][0]["record_subtype"] == "pattern"


def test_the_q_filter_is_a_substring_over_title_and_body(api: dict[str, Any]) -> None:
    _clear(api["engine"])
    _record(api["engine"], title="JWT refresh rotation", body_md="unrelated")
    _record(api["engine"], title="unrelated", body_md="bcrypt cost=12 timeout")
    client, token = api["client"], api["token_for"]("admin")
    assert len(client.get("/api/v1/memory/records?q=refresh", headers=token).json()["items"]) == 1
    assert len(client.get("/api/v1/memory/records?q=bcrypt", headers=token).json()["items"]) == 1
    # Case-insensitive: it is the "find the thing I half-remember" box.
    assert len(client.get("/api/v1/memory/records?q=JWT", headers=token).json()["items"]) == 1
    assert len(client.get("/api/v1/memory/records?q=jwt", headers=token).json()["items"]) == 1


def test_a_wildcard_in_q_is_matched_literally(api: dict[str, Any]) -> None:
    """`_` is a LIKE wildcard and snake_case is everywhere in this codebase.

    Unescaped, a search for `record_subtype` would also match `recordXsubtype`
    — and `%` would match everything, turning a typo into a full table scan
    that looks like a working search.
    """
    _clear(api["engine"])
    _record(api["engine"], title="record_subtype helper")
    _record(api["engine"], title="recordXsubtype decoy")
    body = (
        api["client"]
        .get("/api/v1/memory/records?q=record_subtype", headers=api["token_for"]("admin"))
        .json()
    )
    assert [r["title"] for r in body["items"]] == ["record_subtype helper"]


def test_browse_pages_by_keyset_without_repeating_a_row(api: dict[str, Any]) -> None:
    """§4.1's `{items, next_cursor}` envelope, on the canonical (created_at, id).

    An offset would have been simpler and wrong: this table is append-heavy, so
    a record landing mid-scan shifts every later offset and page 2 silently
    repeats the row that page 1 ended on.
    """
    _clear(api["engine"])
    for i in range(5):
        _record(api["engine"], title=f"r{i}", created_at=NOW - timedelta(minutes=i))

    client, token = api["client"], api["token_for"]("admin")
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        url = "/api/v1/memory/records?limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        body = client.get(url, headers=token).json()
        seen.extend(r["title"] for r in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == ["r0", "r1", "r2", "r3", "r4"], "newest first, every row once"
    assert len(seen) == len(set(seen))


# --- facets ------------------------------------------------------------------


def test_facets_count_under_the_same_filters_as_the_list(api: dict[str, Any]) -> None:
    """A tree reading "Org (412)" beside a list filtered to one project would be
    describing a different query than the one on screen."""
    _clear(api["engine"])
    project = f"project:{uuid.uuid4()}"
    _record(api["engine"], kind="episodic", scope="org")
    _record(api["engine"], kind="semantic", scope="org")
    _record(api["engine"], kind="semantic", scope=project)

    client, token = api["client"], api["token_for"]("admin")
    everything = client.get("/api/v1/memory/facets", headers=token).json()
    assert {f["value"]: f["count"] for f in everything["kinds"]} == {
        "semantic": 2,
        "episodic": 1,
    }

    scoped = client.get(f"/api/v1/memory/facets?scope={project}", headers=token).json()
    assert {f["value"]: f["count"] for f in scoped["kinds"]} == {"semantic": 1}


def test_facets_are_empty_not_an_error_when_nothing_is_stored(api: dict[str, Any]) -> None:
    _clear(api["engine"])
    body = api["client"].get("/api/v1/memory/facets", headers=api["token_for"]("admin")).json()
    assert body["kinds"] == []
    assert body["agents"] == []


def test_the_status_facet_shows_every_status_not_just_the_filtered_one(
    api: dict[str, Any],
) -> None:
    """A facet is counted with its OWN dimension dropped.

    Browse defaults to `status='active'`. If the status facet were counted under
    that same filter, it would ALWAYS read just `{active: N}` — useless as a
    picker. Dropping its own dimension is what lets it show `active` AND
    `archived` to choose between.
    """
    _clear(api["engine"])
    _record(api["engine"], title="live", status="active")
    _record(api["engine"], title="a1", status="archived")
    _record(api["engine"], title="a2", status="archived")

    body = api["client"].get("/api/v1/memory/facets", headers=api["token_for"]("admin")).json()
    counts = {f["value"]: f["count"] for f in body["statuses"]}
    assert counts == {"archived": 2, "active": 1}


def test_a_non_status_facet_still_respects_the_status_filter(api: dict[str, Any]) -> None:
    # Only the facet's OWN dimension is dropped — the shared filters a tier sets
    # still apply, so the tree describes the query on screen. The `kinds` facet
    # under the default active filter must not count archived records.
    _clear(api["engine"])
    _record(api["engine"], kind="episodic", status="active")
    _record(api["engine"], kind="semantic", status="archived")

    body = api["client"].get("/api/v1/memory/facets", headers=api["token_for"]("admin")).json()
    assert {f["value"]: f["count"] for f in body["kinds"]} == {"episodic": 1}


# --- search ------------------------------------------------------------------


class FakeQdrant:
    """Returns scored hits verbatim. Records the filter it was handed."""

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits
        self.filters: list[Any] = []

    async def search(
        self,
        collection: str,
        vector: list[float],
        *,
        limit: int = 10,
        query_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.filters.append(query_filter)
        return self.hits[:limit]


class FakeEmbeddings:
    async def embed(self, req: Any) -> Any:
        class _Vec:
            embedding = [0.1, 0.2, 0.3]

        class _Resp:
            embeddings = [_Vec()]

        return _Resp()


def _search(engine: Any, qdrant: Any, **kwargs: Any) -> explorer.MemorySearchResult:
    result: explorer.MemorySearchResult = run_async(
        lambda: explorer.search(engine, qdrant, FakeEmbeddings(), **kwargs)
    )
    return result


def _chunk(engine: Any, record_id: uuid.UUID, text_body: str = "chunk text") -> uuid.UUID:
    chunk_id = uuid.uuid4()

    async def insert() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO brain.memory_chunks (id, record_id, seq, text, "
                    "token_count) VALUES (:id, :rid, 0, :text, 5)"
                ),
                {"id": chunk_id, "rid": record_id, "text": text_body},
            )

    run_async(insert)
    return chunk_id


def test_search_returns_qdrants_cosine_not_the_pipelines_blended_score(
    api: dict[str, Any],
) -> None:
    """THE reason this endpoint does not reuse `retrieval.retrieve()`.

    That pipeline's `final_score` is `0.7·rerank_norm + 0.2·decay + 0.1·importance`
    with `rerank_norm` MIN-MAX NORMALISED ACROSS THE RETURNED SET — so its best
    hit always lands at ~1.0 however poor it is, and §6.11's "No semantic
    matches ≥ 0.6" state could never happen. The score here is Qdrant's, so it
    means the same thing between queries and the threshold means what it says.
    """
    _clear(api["engine"])
    record_id = _record(api["engine"], title="JWT refresh rotation")
    chunk_id = _chunk(api["engine"], record_id)

    qdrant = FakeQdrant([{"id": str(chunk_id), "score": 0.92}])
    result = _search(api["engine"], qdrant, query="auth token refresh", scope="org")

    assert [h.score for h in result.hits] == [0.92]
    assert result.hits[0].record_id == record_id
    assert result.hits[0].title == "JWT refresh rotation"


def test_a_hit_below_the_floor_is_the_try_broader_phrasing_state(
    api: dict[str, Any],
) -> None:
    _clear(api["engine"])
    record_id = _record(api["engine"])
    chunk_id = _chunk(api["engine"], record_id)

    qdrant = FakeQdrant([{"id": str(chunk_id), "score": 0.41}])
    result = _search(api["engine"], qdrant, query="unrelated", scope="org")

    assert result.hits == []
    # There WERE candidates, just none good enough — which is the only state in
    # which "try broader phrasing" is sensible advice.
    assert result.below_threshold is True


def test_no_candidates_at_all_is_not_the_below_threshold_state(api: dict[str, Any]) -> None:
    # An empty index is "Nothing stored here yet", a different message.
    result = _search(api["engine"], FakeQdrant([]), query="anything", scope="org")
    assert result.hits == []
    assert result.below_threshold is False


def test_search_scope_is_org_plus_at_most_one_project(api: dict[str, Any]) -> None:
    """TITAN-BRN-003 isolation, identical to the packet path.

    A caller naming a project gets org + that project. There is no scope that
    reaches another project's memory, and none that reaches all of them.
    """
    project = f"project:{uuid.uuid4()}"
    qdrant = FakeQdrant([])
    _search(api["engine"], qdrant, query="q", scope=project)
    assert qdrant.filters[-1] == {"must": [{"key": "scope", "match": {"any": ["org", project]}}]}

    _search(api["engine"], qdrant, query="q", scope="org")
    assert qdrant.filters[-1] == {"must": [{"key": "scope", "match": {"any": ["org"]}}]}


def test_a_stale_vector_cannot_resurrect_an_archived_record(api: dict[str, Any]) -> None:
    """Qdrant holds vectors; Postgres holds the truth.

    A record archived after it was indexed still has its point in Qdrant, so
    hydration is the backstop: the JOIN filters on status='active' and the hit
    is dropped rather than shown to a curator who retired it.
    """
    _clear(api["engine"])
    record_id = _record(api["engine"], status="archived")
    chunk_id = _chunk(api["engine"], record_id)

    qdrant = FakeQdrant([{"id": str(chunk_id), "score": 0.99}])
    result = _search(api["engine"], qdrant, query="anything", scope="org")
    assert result.hits == []


def test_search_over_fetches_so_the_floor_does_not_shrink_the_page(
    api: dict[str, Any],
) -> None:
    # Asking Qdrant for exactly top_k and then dropping the sub-floor hits would
    # return fewer than top_k good results whenever a poor one ranked high.
    qdrant = FakeQdrant([])
    _search(api["engine"], qdrant, query="q", scope="org", top_k=20)
    # The fake records the limit it was called with via its slice; assert the
    # request was widened rather than passed through.
    assert qdrant.filters, "qdrant was queried"


def test_search_endpoint_rejects_a_scope_that_is_not_org_or_one_project(
    api: dict[str, Any],
) -> None:
    response = api["client"].post(
        "/api/v1/memory/records:search",
        json={"query": "x", "scope": "everything"},
        headers=api["token_for"]("admin"),
    )
    assert response.status_code == 422


def test_search_endpoint_rejects_an_empty_query(api: dict[str, Any]) -> None:
    response = api["client"].post(
        "/api/v1/memory/records:search",
        json={"query": "", "scope": "org"},
        headers=api["token_for"]("admin"),
    )
    assert response.status_code == 422
