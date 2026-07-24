"""The Memory Explorer's read seams (Vol 4 §6.11, TITAN-FRT-034).

Two things §6.11 needs that the memory module has never exposed: a way to BROWSE
records (faceted by kind, scope, project, agent, date) and a way to SEARCH them
semantically with a score a human can reason about. Provenance and usage history
already exist — ``GET /memory/records/{id}`` returns the record, its chunks and
its complete ledger trail — so FRT-034 needs no new backend, only a screen.

**Why search here is dense-only, and not the §6.1 pipeline.**
``retrieval.retrieve()`` is built for RAG: it fuses a dense and a lexical leg by
RRF (which discards scores in favour of ranks) and then ranks by
``0.7·rerank_norm + 0.2·decay_score + 0.1·importance``, where ``rerank_norm`` is
**min-max normalised across the returned set**. Two consequences make that score
wrong for this screen:

* it is RELATIVE — the best hit in a set normalises to 1.0 however poor it is,
  so the top result always scores ≥ 0.7 and §6.11's "No semantic matches ≥ 0.6"
  state is literally unreachable;
* it is not similarity — a stale-but-relevant record and a fresh-but-irrelevant
  one are deliberately blended, which is right for filling a prompt budget and
  misleading next to a "score" column a human reads.

Qdrant's cosine similarity IS absolute and IS comparable across queries, so the
Explorer asks Qdrant directly. That makes the 0.6 threshold in §6.11 mean what
it says. The trade-off is honest and worth stating: this finds semantic matches
only, so an exact-identifier query that the lexical leg would catch can miss
here — which is why the browse endpoint takes a `q` substring filter too.
"""

import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.gateway.dto import StrictModel
from titan.gateway.pagination import Keyset, Page
from titan.llm.service import EmbeddingsRequest
from titan.memory.ingest import EmbeddingsClient
from titan.memory.models import MEMORY_KINDS, MEMORY_STATUSES
from titan.memory.qdrant import QdrantIndex

#: The dense collection the §6.1 pipeline searches (`retrieval._dense_leg`).
#: `titan_kb` is deliberately not searched here: it holds approved KB documents,
#: which the KB screen owns, not the memory record tiers §6.11 browses.
MEMORY_COLLECTION = "titan_memory"

#: §6.11: "No semantic matches ≥ 0.6 — try broader phrasing". A cosine floor,
#: which is only meaningful because the score is Qdrant's own (see module docs).
DEFAULT_MIN_SCORE = 0.6

#: §6.11: "Results (semantic, top 20)".
DEFAULT_TOP_K = 20
MAX_TOP_K = 100


class MemorySearchRequest(StrictModel):
    """§6.11's search box."""

    query: str = Field(min_length=1, max_length=2_000)
    #: Mandatory isolation (TITAN-BRN-003): 'org', or 'project:<uuid>' for org
    #: PLUS that one project. There is no "everything" scope, by design.
    scope: str = Field(default="org", pattern=r"^(org|project:[0-9a-fA-F-]{36})$")
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)
    #: The cosine floor. §6.11 fixes the copy at 0.6; it is a parameter so the
    #: screen can offer "search harder" without a backend change.
    min_score: float = Field(default=DEFAULT_MIN_SCORE, ge=0.0, le=1.0)


class MemorySearchHit(BaseModel):
    """One scored hit. ``score`` is Qdrant's cosine similarity in [0, 1]."""

    record_id: uuid.UUID
    chunk_id: uuid.UUID
    score: float
    kind: str
    scope: str
    title: str
    #: The matching chunk's text, not the whole record — what the result row
    #: previews. The full body comes from the detail endpoint.
    excerpt: str
    source_agent: str
    project_id: uuid.UUID | None
    created_at: datetime


class MemorySearchResult(BaseModel):
    query: str
    min_score: float
    hits: list[MemorySearchHit]
    #: True when the index answered but nothing cleared the floor — §6.11's
    #: "No semantic matches ≥ 0.6" state, which is distinct from "the index is
    #: empty" and from "the query failed".
    below_threshold: bool = False


class MemoryBrowseRow(BaseModel):
    """A record as the browse list shows it — no body, no chunks."""

    id: uuid.UUID
    kind: str
    scope: str
    status: str
    title: str
    source_agent: str
    source_task_id: uuid.UUID | None
    project_id: uuid.UUID | None
    importance: float
    decay_score: float
    access_count: int
    created_at: datetime
    superseded_by: uuid.UUID | None
    #: §6.11's tier tree calls "Patterns" and "Lessons" tiers. They are not:
    #: they are records discriminated by `metadata->>'record_subtype'`. Surfaced
    #: so the UI can group by it without re-deriving the convention.
    record_subtype: str | None


class MemoryFacet(BaseModel):
    value: str
    count: int


class MemoryFacets(BaseModel):
    """The counts behind §6.11's tier tree and its faceted filters."""

    kinds: list[MemoryFacet]
    scopes: list[MemoryFacet]
    statuses: list[MemoryFacet]
    agents: list[MemoryFacet]
    subtypes: list[MemoryFacet]


@dataclass(frozen=True)
class BrowseFilters:
    kind: str | None = None
    scope: str | None = None
    status: str | None = "active"
    source_agent: str | None = None
    project_id: uuid.UUID | None = None
    subtype: str | None = None
    #: Case-insensitive substring over title/body. The lexical complement to
    #: semantic search, for when you know the identifier.
    q: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None


def validate_kind(kind: str | None) -> None:
    if kind is not None and kind not in MEMORY_KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {', '.join(MEMORY_KINDS)}")


def validate_status(status: str | None) -> None:
    # Validated for the same reason `kind` is: an unknown status is a typo, and
    # an empty result reads as §6.11's "nothing stored here yet" rather than the
    # 422 it should be.
    if status is not None and status not in MEMORY_STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {', '.join(MEMORY_STATUSES)}")


def _where(filters: BrowseFilters) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if filters.kind:
        clauses.append("kind = :kind")
        params["kind"] = filters.kind
    if filters.scope:
        clauses.append("scope = :scope")
        params["scope"] = filters.scope
    if filters.status:
        clauses.append("status = :status")
        params["status"] = filters.status
    if filters.source_agent:
        clauses.append("source_agent = :source_agent")
        params["source_agent"] = filters.source_agent
    if filters.project_id:
        clauses.append("project_id = :project_id")
        params["project_id"] = filters.project_id
    if filters.subtype:
        clauses.append("metadata->>'record_subtype' = :subtype")
        params["subtype"] = filters.subtype
    if filters.q:
        # ILIKE, not the tsv index: the Explorer's box is a "find the thing I
        # half-remember" filter over a page, and a websearch_to_tsquery would
        # not match a partial identifier. `%` and `_` are escaped so a literal
        # underscore in a snake_case identifier is not a wildcard.
        clauses.append("(title ILIKE :q ESCAPE '\\' OR body_md ILIKE :q ESCAPE '\\')")
        params["q"] = f"%{_escape_like(filters.q)}%"
    if filters.created_after:
        clauses.append("created_at >= :created_after")
        params["created_after"] = filters.created_after
    if filters.created_before:
        clauses.append("created_at < :created_before")
        params["created_before"] = filters.created_before
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def browse(
    conn: AsyncConnection,
    *,
    filters: BrowseFilters,
    keyset: Keyset,
) -> Page[MemoryBrowseRow]:
    """A faceted page of records, newest first.

    The canonical `(created_at, id)` keyset (§4.1), not an offset: this table is
    append-heavy, and an offset silently repeats or skips a row whenever
    something lands mid-scan. `Keyset` works here unchanged because the sort IS
    that canonical tuple and the query touches ONE table — so its bare
    `created_at`/`id` cannot be ambiguous, which is the trap the project-runs
    query hit in Phase 61 and had to dodge with a subquery.
    """
    where, params = _where(filters)
    seek, seek_params = keyset.where()
    if seek:
        where = f"{where} AND {seek}" if where else f" WHERE {seek}"
        params |= seek_params

    rows = await conn.execute(
        text(
            "SELECT id, kind, scope, status, title, source_agent, source_task_id, "
            "project_id, importance, decay_score, access_count, created_at, "
            "superseded_by, metadata->>'record_subtype' AS record_subtype "
            f"FROM brain.memory_records{where} "
            f"ORDER BY {keyset.order_by} LIMIT {keyset.fetch}"
        ),
        params,
    )
    page, cursor = keyset.paginate(list(rows.all()))
    return Page(
        items=[MemoryBrowseRow.model_validate(row, from_attributes=True) for row in page],
        next_cursor=cursor,
    )


async def facets(conn: AsyncConnection, *, filters: BrowseFilters) -> MemoryFacets:
    """Counts per facet, under the OTHER filters — not its own.

    Each facet drops its own dimension before counting, the standard faceted-
    search rule: the `statuses` counts are computed as if no status filter were
    applied, so a status picker actually shows `active` AND `archived` to choose
    between. Counting every facet under the full filter (including its own
    dimension) collapses it to a single value — and since browse defaults to
    `status='active'`, the status facet would ALWAYS read just `{active: N}`,
    which is useless as a picker.

    The dimensions that are still shared across all facets — the ones a tier
    selection sets — stay applied, so the tree still describes the query on
    screen rather than the whole table.
    """

    async def count_by(expression: str, others: BrowseFilters) -> list[MemoryFacet]:
        # `others` is `filters` with THIS facet's own dimension already dropped.
        where, params = _where(others)
        rows = await conn.execute(
            text(
                f"SELECT {expression} AS value, COUNT(*) AS count "
                f"FROM brain.memory_records{where} "
                f"GROUP BY {expression} ORDER BY count DESC, value NULLS LAST"
            ),
            params,
        )
        return [
            MemoryFacet(value=str(row.value), count=row.count)
            for row in rows.all()
            if row.value is not None
        ]

    # `replace(..., x=None)` per facet: typed, and drops exactly that facet's own
    # dimension so its counts show every option rather than collapsing to one.
    return MemoryFacets(
        kinds=await count_by("kind", replace(filters, kind=None)),
        scopes=await count_by("scope", replace(filters, scope=None)),
        statuses=await count_by("status", replace(filters, status=None)),
        agents=await count_by("source_agent", replace(filters, source_agent=None)),
        subtypes=await count_by("metadata->>'record_subtype'", replace(filters, subtype=None)),
    )


async def search(
    engine: AsyncEngine,
    qdrant: QdrantIndex,
    embeddings: EmbeddingsClient,
    *,
    query: str,
    scope: str,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> MemorySearchResult:
    """Semantic search for a human (§6.11).

    Scope isolation is mandatory and identical to the packet path
    (TITAN-BRN-003): org, plus at most the one named project. A caller cannot
    widen it.
    """
    response = await embeddings.embed(EmbeddingsRequest(input=[query]))
    vector = response.embeddings[0].embedding
    # Identical to `retrieval._dense_filter`'s scope clause, and mandatory for
    # the same reason (TITAN-BRN-003): org plus at most the one named project,
    # never another project's memory.
    scopes = ("org",) if scope == "org" else ("org", scope)
    hits = await qdrant.search(
        MEMORY_COLLECTION,
        vector,
        # Over-fetch, because the floor is applied AFTER Qdrant returns: asking
        # for exactly top_k and then dropping everything under 0.6 would return
        # fewer than 20 good hits whenever a few poor ones ranked high.
        limit=min(top_k * 3, MAX_TOP_K * 3),
        query_filter={"must": [{"key": "scope", "match": {"any": list(scopes)}}]},
    )

    scored = [(hit, float(hit.get("score", 0.0))) for hit in hits]
    keep = [(hit, score) for hit, score in scored if score >= min_score]
    if not keep:
        # Distinguish "found nothing good enough" from "found nothing at all":
        # §6.11 words the first one "try broader phrasing", which is only
        # sensible advice when there WERE candidates.
        return MemorySearchResult(
            query=query, min_score=min_score, hits=[], below_threshold=bool(scored)
        )

    by_chunk = {uuid.UUID(str(hit["id"])): score for hit, score in keep}
    rows = await _hydrate(engine, list(by_chunk))
    out = [
        MemorySearchHit(
            record_id=row.record_id,
            chunk_id=row.chunk_id,
            score=by_chunk[row.chunk_id],
            kind=row.kind,
            scope=row.scope,
            title=row.title,
            excerpt=row.text,
            source_agent=row.source_agent,
            project_id=row.project_id,
            created_at=row.created_at,
        )
        for row in rows
    ]
    out.sort(key=lambda h: (-h.score, str(h.chunk_id)))
    return MemorySearchResult(query=query, min_score=min_score, hits=out[:top_k])


async def _hydrate(engine: AsyncEngine, chunk_ids: list[uuid.UUID]) -> list[Any]:
    """Qdrant holds vectors and ids; Postgres holds the truth.

    Also the authorisation backstop: a chunk whose record has been archived or
    deleted since it was indexed is dropped here, so a stale vector cannot
    resurrect a record the curator retired.
    """
    if not chunk_ids:
        return []
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT c.id AS chunk_id, c.record_id, c.text, r.kind, r.scope, r.title, "
                "r.source_agent, r.project_id, r.created_at "
                "FROM brain.memory_chunks c "
                "JOIN brain.memory_records r ON r.id = c.record_id "
                "WHERE c.id = ANY(:ids) AND r.status = 'active'"
            ),
            {"ids": chunk_ids},
        )
        return list(rows.all())
