"""Pattern mining + project→org promotion (Vol 5 §11.1, §11.3,
TITAN-BRN-023/024) — private to the `memory` module.

- **Pattern miner** (§11.1): a milestone-triggered job that collects a project's
  decision records, asks the cheapest model tier (TITAN-BRN-017) for candidate
  patterns, keeps only those NOVEL against org memory (dense cosine < 0.85), and
  writes each as a project-scope ``pattern`` record with DERIVED_FROM evidence
  edges. The run is visible in the ledger.
- **Promotion** (§11.3): a lesson/pattern is promoted to org scope only on the
  multi-signal rule (TITAN-BRN-024) — observed in ≥ 2 projects, or an approved
  org-candidate, or a human mark. Promotion clones the record into org scope with
  a provenance edge (``promote`` ledger action); procedural content additionally
  opens a KB proposal through the Phase-39 workflow. Direct org-scope writes from
  agent credentials stay rejected (write_events / the HTTP surface).
"""

import json
import re
import uuid
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.db import uuid7
from titan.gateway.problems import ProblemException, problem_type
from titan.llm.service import ChatRequest, EmbeddingsRequest, Message, RequestMetadata
from titan.memory import kg
from titan.memory import repository as repo
from titan.memory.qdrant import QdrantIndex
from titan.memory.reflection import _write_episodic

logger = structlog.get_logger("titan.memory.patterns")

NOVELTY_THRESHOLD = 0.85  # §11.1: a candidate scoring ≥ this vs org memory is NOT novel
MINER_MAX_TOKENS = 800


# --- pattern miner (§11.1, TITAN-BRN-023) ------------------------------------


class MinedPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: uuid.UUID
    name: str


class MinerReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions_scanned: int
    candidates: int
    novel: int
    patterns: list[MinedPattern]


_MINER_PROMPT = (
    "You are the pattern miner. From these decision records, propose recurring "
    "reusable patterns (architecture, fixes, or rejections). Reply with ONLY a JSON "
    "array of objects with keys `name` (short) and `description` (one sentence). "
    "Propose at most 5.\n\nDecision records:\n{decisions}"
)


async def mine_patterns(
    engine: AsyncEngine,
    gateway: Any,
    qdrant: QdrantIndex,
    embeddings: Any,
    *,
    project_id: uuid.UUID,
    model_alias: str | None,
) -> MinerReport:
    """§11.1: mine novel patterns from a project's decisions and persist them as
    project-scope ``pattern`` records with evidence edges (TITAN-BRN-023). The
    LLM runs on the cheapest tier; novelty is a direct dense-similarity check
    against org memory."""
    scope = f"project:{project_id}"
    async with engine.connect() as conn:
        decisions = list(await repo.decision_records_for_project(conn, project_id))
    if not decisions:
        await _log_miner_run(engine, project_id, scanned=0, candidates=0, novel=[])
        return MinerReport(decisions_scanned=0, candidates=0, novel=0, patterns=[])
    joined = "\n".join(f"- {d.title}: {d.body_md[:300]}" for d in decisions)
    response = await gateway.chat(
        ChatRequest(
            messages=[Message(role="user", content=_MINER_PROMPT.format(decisions=joined[:8000]))],
            model_alias=model_alias,
            max_tokens=MINER_MAX_TOKENS,
        )
    )
    candidates = _parse_candidates(response.text or "")
    evidence = [kg.KgNodeRef(node_type="decision", name=d.title[:200]) for d in decisions[:5]]
    minted: list[MinedPattern] = []
    novel = 0
    for cand in candidates:
        name = str(cand.get("name", "")).strip()
        description = str(cand.get("description", "")).strip()
        if not name or not description:
            continue
        if not await _is_novel(qdrant, embeddings, f"{name}. {description}", project_id):
            continue
        novel += 1
        record_id = uuid7()
        async with engine.begin() as conn:
            await _write_episodic(
                conn,
                record_id=record_id,
                scope=scope,
                title=name,
                body_md=f"# {name}\n\n{description}",
                metadata={"record_subtype": "pattern", "miner": True},
                importance=0.6,
                source_agent="agent.memory_agent",
                actor_agent="agent.memory_agent",
                source_task_id=None,
                kind="semantic",
            )
            pattern_node = await kg.ensure_node(
                conn, scope=scope, node_type="pattern", name=name, record_id=record_id
            )
            for ref in evidence:
                decision_node = await kg.ensure_node(
                    conn, scope=scope, node_type=ref.node_type, name=ref.name
                )
                await kg.add_edge(
                    conn, src=pattern_node, dst=decision_node, edge_type="DERIVED_FROM"
                )
        minted.append(MinedPattern(record_id=record_id, name=name))
    # TITAN-BRN-023: the miner RUN is visible in the ledger regardless of whether
    # it minted anything (per-pattern 'write' rows alone would hide a 0-novel run).
    await _log_miner_run(
        engine, project_id, scanned=len(decisions), candidates=len(candidates),
        novel=[p.record_id for p in minted],
    )
    logger.info("memory.pattern_miner_run", project_id=str(project_id), novel=novel)
    return MinerReport(
        decisions_scanned=len(decisions),
        candidates=len(candidates),
        novel=novel,
        patterns=minted,
    )


async def _log_miner_run(
    engine: AsyncEngine,
    project_id: uuid.UUID,
    *,
    scanned: int,
    candidates: int,
    novel: list[uuid.UUID],
) -> None:
    """One ledger row per miner run (TITAN-BRN-023) — counts only, no content."""
    async with engine.begin() as conn:
        await repo.append_ledger(
            conn,
            actor_agent="agent.memory_agent",
            task_id=None,
            action="write",
            record_ids=novel,
            detail=json.dumps(
                {"miner_run": True, "scope": f"project:{project_id}",
                 "decisions_scanned": scanned, "candidates": candidates, "novel": len(novel)}
            ),
        )


def _parse_candidates(text: str) -> list[dict[str, Any]]:
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match is None:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return [item for item in parsed if isinstance(item, dict)]


async def _is_novel(qdrant: QdrantIndex, embeddings: Any, text: str, project_id: uuid.UUID) -> bool:
    """Novel = no ORG memory point within cosine ≥ 0.85 (§11.1). Best-effort: an
    empty/unreachable index counts as novel (nothing to conflict with)."""
    try:
        response = await embeddings.embed(
            EmbeddingsRequest(input=[text], metadata=RequestMetadata(agent_id="memory.miner"))
        )
        hits = await qdrant.search(
            "titan_memory",
            response.embeddings[0].embedding,
            limit=1,
            query_filter={"must": [{"key": "scope", "match": {"any": ["org"]}}]},
        )
    except Exception:
        logger.warning("memory.miner_novelty_check_skipped")
        return True
    if not hits:
        return True
    return float(hits[0].get("score", 0.0)) < NOVELTY_THRESHOLD


# --- promotion (§11.3, TITAN-BRN-024) ----------------------------------------


class PromotionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_record_id: uuid.UUID
    org_record_id: uuid.UUID
    signal: str
    kb_proposal_id: uuid.UUID | None = None


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: uuid.UUID
    human_marked: bool = False
    org_candidate_approved: bool = False


async def promote_record(
    engine: AsyncEngine,
    qdrant: QdrantIndex,
    embeddings: Any,
    request: PromotionRequest,
    *,
    actor_agent: str,
) -> PromotionResult:
    """Promote a project-scope lesson/pattern to org scope on the multi-signal
    rule (§11.3, TITAN-BRN-024): observed in ≥ 2 projects, OR an approved
    org-candidate, OR a human mark. Clones into org scope with a ``promote``
    ledger row; procedural content opens a KB proposal. Rejected (409) when no
    signal is present — org memory never fills on a single project's say-so."""
    async with engine.connect() as conn:
        row = await repo.get_record(conn, request.record_id)
        if row is None:
            raise ProblemException(
                status=404,
                title="Not found",
                detail="No such memory record.",
                problem_type=problem_type("not-found"),
            )
        if row.scope == "org":
            raise _conflict("record is already org-scope.")
        cross = await repo.scope_near_duplicate_projects(
            conn, title=row.title, exclude_scope=row.scope
        )
    signal = _promotion_signal(request, cross)
    if signal is None:
        raise _conflict(
            "no promotion signal (need ≥2 projects, org-candidate approval, or a human mark)."
        )

    org_record_id = uuid7()
    metadata = _clone_metadata(row.metadata, source_id=row.id, signal=signal)
    async with engine.begin() as conn:
        await _write_episodic(
            conn,
            record_id=org_record_id,
            scope="org",
            title=row.title,
            body_md=row.body_md,
            metadata=metadata,
            importance=float(row.importance),
            source_agent=actor_agent,
            actor_agent=actor_agent,
            source_task_id=None,
            kind=row.kind,
        )
        # 'promote' provenance in the ledger (in addition to the write row).
        await repo.append_ledger(
            conn,
            actor_agent=actor_agent,
            task_id=None,
            action="promote",
            record_ids=[org_record_id, row.id],
            detail=json.dumps({"signal": signal, "from_scope": row.scope}),
        )
        # Cross-scope provenance: the org node SUPERSEDES the project node.
        subtype = _subtype_of(row.metadata)
        if subtype in ("lesson", "pattern"):
            org_node = await kg.ensure_node(
                conn, scope="org", node_type=subtype, name=row.title, record_id=org_record_id
            )
            src_node = await repo.get_kg_node_id(
                conn, scope=row.scope, node_type=subtype, name=row.title
            )
            if src_node is not None:
                await kg.add_edge(conn, src=org_node, dst=src_node, edge_type="SUPERSEDES")

    kb_proposal_id = None
    if row.kind == "procedural":
        # Procedural content hardens into a standard/best practice (§11.3, §10.3).
        from titan.memory import kb  # noqa: PLC0415 — avoid a module-load cycle

        proposal = await kb.create_proposal(
            engine,
            qdrant,
            embeddings,
            kb.KbProposalIn(
                doc_type="best_practice",
                slug=_slugify(row.title),
                title=row.title,
                body_md=row.body_md,
                change_note=f"promoted from {row.scope} on signal '{signal}'",
                rationale="org promotion of recurring procedural knowledge",
            ),
            proposed_by=actor_agent,
        )
        kb_proposal_id = proposal.id
    return PromotionResult(
        source_record_id=row.id,
        org_record_id=org_record_id,
        signal=signal,
        kb_proposal_id=kb_proposal_id,
    )


def _promotion_signal(request: PromotionRequest, other_project_count: int) -> str | None:
    # other_project_count is DISTINCT projects OTHER than the source that hold the
    # same record; ≥1 other means "observed in ≥2 projects" total (§11.3).
    if other_project_count >= 1:
        return "two-projects"
    if request.org_candidate_approved:
        return "org-candidate-approved"
    if request.human_marked:
        return "human-marked"
    return None


def _subtype_of(metadata: Any) -> str | None:
    data = metadata if isinstance(metadata, dict) else json.loads(metadata or "{}")
    subtype = data.get("record_subtype") if isinstance(data, dict) else None
    return str(subtype) if subtype else None


def _clone_metadata(metadata: Any, *, source_id: uuid.UUID, signal: str) -> dict[str, Any]:
    data = metadata if isinstance(metadata, dict) else json.loads(metadata or "{}")
    cloned = dict(data)
    cloned["promoted_from"] = str(source_id)
    cloned["promotion_signal"] = signal
    return cloned


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug or "promoted-doc")[:200]


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type=problem_type("conflict")
    )


__all__ = [
    "NOVELTY_THRESHOLD",
    "MinedPattern",
    "MinerReport",
    "PromotionRequest",
    "PromotionResult",
    "mine_patterns",
    "promote_record",
]
