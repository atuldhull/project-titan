"""Knowledge Base internals (Vol 5 §10, TITAN-BRN-021/022) — private to `memory`.

The KB is the curated, versioned core of procedural org memory. This module owns
the §10.3 write path — agents NEVER mutate the KB directly; every change is a
proposal that a router approves — and the materialisation that makes an approved
version retrievable:

- a proposal (new doc or new version) carries a diff + rationale; the service
  validates format, runs a semantic near-duplicate conflict check, and routes by
  type (``coding_standard``/``decision_record`` need human sign-off; others need
  Code-Reviewer approval only);
- on approval the new version becomes current, is MATERIALISED as a procedural,
  org-scope ``memory_record`` whose chunks flow through the SAME §5.1 pipeline
  into the ``titan_kb`` collection (via ``ingest_outbox.collection``), the prior
  version's points are superseded out, and KG provenance is written (§10.3);
- pin-by-slug (TITAN-BRN-022) reads the current version's chunks straight from
  PostgreSQL — deterministic, no ranking, available the instant a version is
  approved (before embedding even completes).

Every version row retains proposer, approver, and change note (TITAN-BRN-021).
Direct KB-table mutation via the API is a flat 403 (the router's tarpit routes).
"""

import json
import re
import uuid
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.db import uuid7
from titan.gateway.problems import ProblemException, problem_type
from titan.llm.service import EmbeddingsRequest, RequestMetadata
from titan.memory import kg
from titan.memory import repository as repo
from titan.memory.chunker import ChunkContext, get_chunker
from titan.memory.ingest import EmbeddingsClient
from titan.memory.qdrant import QdrantIndex

logger = structlog.get_logger("titan.memory.kb")

# §10.1 — the six content types.
DOC_TYPES = (
    "coding_standard",
    "architecture_pattern",
    "best_practice",
    "template",
    "decision_record",
    "api_reference_note",
)
# §10.3 routing: these require a human sign-off; the rest, Code-Reviewer approval
# only (see the router's approver gate).
PROTECTED_DOC_TYPES = frozenset({"coding_standard", "decision_record"})

# KB is org-wide curated memory (kb_documents has no scope column) and is stored
# as PROCEDURAL memory — the kind that routes the §6.1 dense leg to titan_kb.
KB_SCOPE = "org"
KB_KIND = "procedural"
KB_IMPORTANCE = 0.8
MAX_BODY_MD_CHARS = 200_000  # mirrors the memory-record body cap (tsvector limit)

# The cosine score above which an existing KB doc counts as a near-duplicate.
NEAR_DUP_THRESHOLD = 0.9
_NEAR_DUP_PROBE_CHARS = 2_000

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


# --- DTOs --------------------------------------------------------------------


class KbProposalIn(BaseModel):
    """POST /kb/proposals — a new doc or a new version of an existing slug."""

    model_config = ConfigDict(extra="forbid")

    doc_type: str
    slug: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=1000)
    body_md: str = Field(min_length=1, max_length=MAX_BODY_MD_CHARS)
    change_note: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=8000)
    tags: list[str] = Field(default_factory=list)
    language: str | None = Field(default=None, max_length=100)

    @field_validator("doc_type")
    @classmethod
    def _validate_doc_type(cls, value: str) -> str:
        if value not in DOC_TYPES:
            raise ValueError(f"doc_type must be one of {DOC_TYPES}")
        return value

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                "slug must be lowercase alphanumeric words separated by single hyphens"
            )
        return value


class NearDuplicate(BaseModel):
    slug: str
    score: float


class KbProposalOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    document_id: uuid.UUID | None
    doc_type: str
    slug: str
    title: str
    status: str
    requires_human: bool
    near_duplicates: list[NearDuplicate]
    created_at: Any


class KbApprovalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal_id: uuid.UUID
    document_id: uuid.UUID
    slug: str
    version: int
    record_id: uuid.UUID
    chunk_count: int
    kb_doc_node_id: uuid.UUID


class KbVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    version: int
    body_md: str
    change_note: str
    proposed_by: str
    approved_by: str | None
    created_at: Any


class KbDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doc_type: str
    slug: str
    title: str
    current_version: int
    status: str
    tags: list[str]
    created_at: Any


class KbDocumentDetail(BaseModel):
    document: KbDocumentOut
    versions: list[KbVersionOut]


def _validation_error(detail: str) -> ProblemException:
    return ProblemException(
        status=422, title="Invalid request", detail=detail, problem_type=problem_type("validation")
    )


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type=problem_type("conflict")
    )


# --- proposal (§10.3 write path, step 1–2) -----------------------------------


async def _near_duplicates(
    engine: AsyncEngine,
    qdrant: QdrantIndex,
    embeddings: EmbeddingsClient,
    proposal: KbProposalIn,
) -> list[NearDuplicate]:
    """The §10.3 semantic near-duplicate conflict check: embed the proposal and
    search ``titan_kb`` for existing docs above the similarity threshold (a
    different slug). Best-effort — a missing/empty collection or a transient
    Qdrant error yields no conflicts rather than blocking the proposal."""
    probe = f"{proposal.title}\n{proposal.body_md[:_NEAR_DUP_PROBE_CHARS]}"
    try:
        response = await embeddings.embed(
            EmbeddingsRequest(input=[probe], metadata=RequestMetadata(agent_id="memory.kb"))
        )
        hits = await qdrant.search(
            "titan_kb",
            response.embeddings[0].embedding,
            limit=5,
            query_filter={"must": [{"key": "scope", "match": {"any": [KB_SCOPE]}}]},
        )
    except Exception:
        logger.warning("memory.kb_near_dup_check_skipped")
        return []
    candidates: dict[uuid.UUID, float] = {}
    for hit in hits:
        payload = hit.get("payload") or {}
        record_raw = payload.get("record_id")
        if record_raw is None:
            continue
        score = float(hit.get("score", 0.0))
        if score >= NEAR_DUP_THRESHOLD:
            rid = uuid.UUID(str(record_raw))
            candidates[rid] = max(candidates.get(rid, 0.0), score)
    if not candidates:
        return []
    async with engine.connect() as conn:
        slugs = await repo.kb_slugs_for_records(conn, list(candidates))
    dups: dict[str, float] = {}
    for rid, score in candidates.items():
        slug = slugs.get(rid)
        if slug and slug != proposal.slug:
            dups[slug] = max(dups.get(slug, 0.0), score)
    return [NearDuplicate(slug=slug, score=round(score, 6)) for slug, score in dups.items()]


async def create_proposal(
    engine: AsyncEngine,
    qdrant: QdrantIndex,
    embeddings: EmbeddingsClient,
    proposal: KbProposalIn,
    *,
    proposed_by: str,
) -> KbProposalOut:
    """Register a KB proposal (§10.3 step 1–2): validate, run the near-duplicate
    conflict check, resolve whether it targets a new doc or a new version, and
    persist it ``pending`` with its routing (``requires_human``). Never mutates a
    KB table directly — approval does that."""
    near_dups = await _near_duplicates(engine, qdrant, embeddings, proposal)
    async with engine.begin() as conn:
        existing = await repo.get_kb_document_by_slug(conn, proposal.slug)
        document_id: uuid.UUID | None = None
        if existing is not None:
            if existing.doc_type != proposal.doc_type:
                raise _validation_error(
                    f"slug '{proposal.slug}' already exists as doc_type "
                    f"'{existing.doc_type}'; a slug's type is immutable."
                )
            document_id = existing.id
        row = await repo.insert_kb_proposal(
            conn,
            document_id=document_id,
            doc_type=proposal.doc_type,
            slug=proposal.slug,
            title=proposal.title,
            body_md=proposal.body_md,
            change_note=proposal.change_note,
            rationale=proposal.rationale,
            tags=proposal.tags,
            proposed_by=proposed_by,
            requires_human=proposal.doc_type in PROTECTED_DOC_TYPES,
            near_duplicates=json.dumps([nd.model_dump() for nd in near_dups]),
        )
    return _proposal_out(row)


# --- approval (§10.3 write path, step 3) -------------------------------------


async def approve_proposal(
    engine: AsyncEngine,
    proposal_id: uuid.UUID,
    *,
    approver: str,
    approver_is_human: bool,
) -> KbApprovalResult:
    """Approve a pending proposal (§10.3 step 3, TITAN-BRN-021): make the new
    version current, materialise it into ``titan_kb`` through the §5.1 pipeline,
    supersede the prior version's points, and write KG provenance — all in one
    transaction. Routing: a protected type (coding_standard/decision_record)
    demands a human approver (403 otherwise); the whole thing is idempotent
    against a lost proposal (the pending-guarded decision claims it exactly
    once)."""
    async with engine.connect() as conn:
        proposal = await repo.get_kb_proposal(conn, proposal_id)
    if proposal is None:
        raise ProblemException(
            status=404,
            title="Not found",
            detail="No such KB proposal.",
            problem_type=problem_type("not-found"),
        )
    if proposal.status != "pending":
        raise _conflict(f"proposal is already {proposal.status}.")
    if proposal.requires_human and not approver_is_human:
        raise ProblemException(
            status=403,
            title="Forbidden",
            detail=(
                f"doc_type '{proposal.doc_type}' requires a human sign-off "
                "(TITAN-BRN-021 §10.3); a Code-Reviewer agent cannot approve it alone."
            ),
            problem_type=problem_type("forbidden"),
        )

    tags = list(proposal.tags or [])
    record_id = uuid7()  # the new version's backing memory_record
    try:
        async with engine.begin() as conn:
            document_id = proposal.document_id
            previous_record_id: uuid.UUID | None = None
            if document_id is None:
                created = await repo.insert_kb_document(
                    conn,
                    doc_type=proposal.doc_type,
                    slug=proposal.slug,
                    title=proposal.title,
                    tags=tags,
                )
                document_id = created.id
                version = 1
            else:
                existing = await repo.get_kb_document(conn, document_id)
                if existing is None:  # pragma: no cover — FK guarantees it
                    raise _conflict("the proposal's document no longer exists.")
                version = int(existing.current_version) + 1
                # Capture the OLD current version's record BEFORE bumping the
                # pointer; it is superseded once the NEW record exists (below).
                previous = await repo.current_kb_version(conn, document_id)
                if previous is not None:
                    previous_record_id = previous.record_id
                await repo.bump_kb_document(
                    conn, document_id, version=version, title=proposal.title, tags=tags
                )
            # Materialise the new version FIRST so the superseded_by FK below has
            # a target that exists.
            chunk_count = await _materialise_version(
                conn,
                record_id=record_id,
                proposal=proposal,
                document_id=document_id,
                version=version,
                approver=approver,
                tags=tags,
            )
            if previous_record_id is not None:
                # Retire the prior version's points out of titan_kb (§10.2).
                await repo.supersede_record(
                    conn,
                    record_id=previous_record_id,
                    superseded_by=record_id,
                    collection="titan_kb",
                )
            await repo.insert_kb_version(
                conn,
                document_id=document_id,
                version=version,
                body_md=proposal.body_md,
                change_note=proposal.change_note,
                proposed_by=proposal.proposed_by,
                approved_by=approver,
                record_id=record_id,
            )
            kb_doc_node = await kg.write_kb_provenance(
                conn,
                scope=KB_SCOPE,
                slug=proposal.slug,
                title=proposal.title,
                approved_by=approver,
                record_id=record_id,
            )
            claimed = await repo.decide_kb_proposal(
                conn,
                proposal_id,
                status="approved",
                decided_by=approver,
                resulting_version=version,
            )
            if not claimed:
                raise _conflict("proposal was already decided by a concurrent approval.")
    except IntegrityError as exc:
        # A concurrent approval of a DIFFERENT proposal for the same NEW slug
        # won the UNIQUE(slug) race — a clean 409, not a 500.
        raise _conflict(f"a document with slug '{proposal.slug}' already exists.") from exc
    return KbApprovalResult(
        proposal_id=proposal_id,
        document_id=document_id,
        slug=proposal.slug,
        version=version,
        record_id=record_id,
        chunk_count=chunk_count,
        kb_doc_node_id=kb_doc_node,
    )


async def _materialise_version(
    conn: AsyncConnection,
    *,
    record_id: uuid.UUID,
    proposal: Any,
    document_id: uuid.UUID,
    version: int,
    approver: str,
    tags: list[str],
) -> int:
    """Write the approved version as a procedural org-scope ``memory_record``,
    chunk it, and enqueue its chunks for embedding into ``titan_kb`` (the SAME
    §5.1 pipeline — no bespoke embed code). Appends one ``write`` ledger row
    attributed to the approver (TITAN-BRN-005). Returns the chunk count."""
    metadata: dict[str, Any] = {
        "kb": True,
        "kb_slug": proposal.slug,
        "kb_document_id": str(document_id),
        "kb_version": version,
        "doc_type": proposal.doc_type,
        "tags": tags,
        "change_note": proposal.change_note,
    }
    language = _language_of(proposal)
    if language:
        metadata["language"] = language
    await repo.insert_record(
        conn,
        record_id=record_id,
        kind=KB_KIND,
        scope=KB_SCOPE,
        project_id=None,
        source_agent=proposal.proposed_by,
        source_task_id=None,
        title=proposal.title,
        body_md=proposal.body_md,
        metadata=json.dumps(metadata),
        importance=KB_IMPORTANCE,
    )
    ctx = ChunkContext(scope=KB_SCOPE, kind=KB_KIND, title=proposal.title)
    chunks = get_chunker("markdown").chunk(proposal.body_md, ctx)
    for chunk in chunks:
        chunk_id = uuid7()
        await repo.insert_chunk(
            conn,
            chunk_id=chunk_id,
            record_id=record_id,
            seq=chunk.seq,
            chunk_text=chunk.text,
            token_count=chunk.token_count,
        )
        await repo.insert_outbox(conn, chunk_id=chunk_id, op="upsert", collection="titan_kb")
    await repo.append_ledger(
        conn,
        actor_agent=approver,
        task_id=None,
        action="write",
        record_ids=[record_id],
        detail=json.dumps({"kb_slug": proposal.slug, "kb_version": version, "chunks": len(chunks)}),
    )
    return len(chunks)


def _language_of(proposal: Any) -> str | None:
    """The proposal's ``language`` field when present (proposals from the API)
    else None (a persisted-row proposal has no language column)."""
    return getattr(proposal, "language", None)


# --- reads (Memory Explorer — older versions stay queryable, §10.2) ----------


async def get_document_detail(engine: AsyncEngine, slug: str) -> KbDocumentDetail | None:
    async with engine.connect() as conn:
        doc = await repo.get_kb_document_by_slug(conn, slug)
        if doc is None:
            return None
        versions = await repo.list_kb_versions(conn, doc.id)
    return KbDocumentDetail(
        document=KbDocumentOut.model_validate(doc),
        versions=[KbVersionOut.model_validate(v) for v in versions],
    )


# --- shared -----------------------------------------------------------------


def _proposal_out(row: Any) -> KbProposalOut:
    raw = row.near_duplicates
    near = raw if isinstance(raw, list) else json.loads(raw or "[]")
    return KbProposalOut(
        id=row.id,
        document_id=row.document_id,
        doc_type=row.doc_type,
        slug=row.slug,
        title=row.title,
        status=row.status,
        requires_human=row.requires_human,
        near_duplicates=[NearDuplicate.model_validate(nd) for nd in near],
        created_at=row.created_at,
    )


__all__ = [
    "DOC_TYPES",
    "KB_IMPORTANCE",
    "KB_KIND",
    "KB_SCOPE",
    "PROTECTED_DOC_TYPES",
    "KbApprovalResult",
    "KbDocumentDetail",
    "KbDocumentOut",
    "KbProposalIn",
    "KbProposalOut",
    "KbVersionOut",
    "NearDuplicate",
    "approve_proposal",
    "create_proposal",
    "get_document_detail",
]
