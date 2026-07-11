"""Memory Service REST endpoints (Vol 5 §14): write Memory Events (batch,
transactional) and fetch a record with its chunks + ledger trail. On the Phase-7
framework, guarded by Phase-9 ``Requires``. Private to the module; main.py
attaches it via mount().

These are internal service endpoints. Writes are agent operations
(``memory:write``); reads are ``memory:read`` (covered by ``*:read``). The
``source_agent`` on the write batch is application data — WHICH agent role
authored the memory — while the ledger's ``actor_agent`` is the authenticated
principal performing the operation, so provenance and audit are both recorded.
"""

import uuid
from typing import Annotated, cast

import httpx
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import (
    PermissionResolver,
    Requires,
    RequiresAuth,
    forbidden,
    get_resolver,
    not_found,
    permission_granted,
)
from titan.identity.service import Principal
from titan.memory import service
from titan.memory.service import (
    CurationReport,
    EmbeddingsClient,
    KbApprovalResult,
    KbDocumentDetail,
    KbProposalIn,
    KbProposalOut,
    KgQueryRequest,
    KgQueryResult,
    LessonIn,
    LessonResult,
    MemoryEventBatch,
    MemoryMetricsOut,
    MemoryPacketOut,
    MemoryRecordDetail,
    MemoryWriteResult,
    MinerReport,
    PacketRequest,
    PromotionRequest,
    PromotionResult,
    QdrantIndex,
    RebuildReport,
    ReflectionResult,
    Reranker,
    SprintRetroResult,
)

router = APIRouter(prefix="/api/v1", tags=["memory"])

_Authenticated = Annotated[Principal, Depends(RequiresAuth())]
_ReadMemory = Annotated[Principal, Depends(Requires("memory:read"))]
_AdminMemory = Annotated[Principal, Depends(Requires("memory:admin"))]


def _actor(principal: Principal) -> str:
    """The audit actor string for the ledger: the agent role for an agent
    principal, else the user id (never spoofable client input)."""
    if principal.kind == "agent" and principal.roles:
        return principal.roles[0]
    return str(principal.sub)


@router.post("/memory/events", response_model=MemoryWriteResult, status_code=201)
async def write_memory_events(
    request: Request,
    body: MemoryEventBatch,
    principal: _Authenticated,
) -> MemoryWriteResult:
    """Write a batch of Memory Events in ONE transaction — a failure anywhere
    rolls back the whole batch (§5.1). ``source_agent`` records WHICH agent role
    authored the memory (application data); the ledger's actor is the
    authenticated principal, never spoofable body input (TITAN-BRN-005).

    Authorization is scope-aware (TITAN-BRN-024): a human/service principal
    needs org-level ``memory:write`` as before; an AGENT principal may write
    ONLY project-scoped events into a project where its role bundle grants
    ``memory:write`` (its bound project) — an org-scope event from agent
    credentials is a flat 403, because org promotion belongs to the curation
    workflow's multi-signal rule, never to a single agent."""
    resolver = get_resolver(request)
    if principal.kind == "agent":
        for event in body.events:
            if event.scope == "org":
                raise ProblemException(
                    status=403,
                    title="Forbidden",
                    detail=(
                        "Agent credentials cannot write org-scope memory records (TITAN-BRN-024)."
                    ),
                    problem_type=problem_type("forbidden"),
                )
            project_id = service.parse_scope(event.scope)
            assert project_id is not None  # noqa: S101 — scope validator guarantees it
            granted = await resolver.project_permissions(project_id)
            if not permission_granted("memory:write", granted):
                raise forbidden("memory:write")
    elif not await resolver.has("memory:write"):
        raise forbidden("memory:write")
    async with request.app.state.engine.begin() as conn:
        return await service.write_events(
            conn, body, source_agent=body.source_agent, actor_agent=_actor(principal)
        )


@router.get("/memory/records/{record_id}", response_model=MemoryRecordDetail)
async def get_memory_record(
    request: Request,
    record_id: uuid.UUID,
    principal: _ReadMemory,
    task_id: Annotated[uuid.UUID | None, Query()] = None,
) -> MemoryRecordDetail:
    """The canonical record + its chunks + its complete ledger trail (§14). A read
    bumps access metadata and appends a ``read`` ledger row (TITAN-BRN-005), so it
    runs in a transaction."""
    async with request.app.state.engine.begin() as conn:
        detail = await service.get_record_detail(
            conn, record_id, actor_agent=_actor(principal), task_id=task_id
        )
    if detail is None:
        raise not_found("memory record")
    return detail


@router.post("/memory/packets", response_model=MemoryPacketOut)
async def assemble_memory_packet(
    request: Request,
    body: PacketRequest,
    principal: _ReadMemory,
) -> MemoryPacketOut:
    """Assemble a cited §6.2 Memory Packet through the §6.1 hybrid retrieval
    pipeline (dense ∥ lexical → RRF → rerank → budget fill), served from the
    300 s Redis cache for identical (scope, query) requests (TITAN-BRN-012).
    Scope isolation is mandatory: org + the caller's named project only
    (TITAN-BRN-003)."""
    state = request.app.state
    return await service.assemble_packet(
        state.engine,
        state.redis,
        QdrantIndex(cast(httpx.AsyncClient, state.http)),
        cast(EmbeddingsClient, state.llm_gateway),
        cast(Reranker, state.memory_reranker),
        body,
    )


@router.get("/memory/metrics", response_model=MemoryMetricsOut)
async def memory_metrics(principal: _ReadMemory) -> MemoryMetricsOut:
    """Retrieval-quality and ingestion-lag metrics (§14) — the same series the
    Prometheus ``/metrics`` endpoint exports, summarised for dashboards and
    operators: packet-cache hit rate (TITAN-BRN-012) and ingest-lag quantiles
    (TITAN-BRN-007). Metadata only — counts and seconds, never content."""
    return service.memory_metrics()


@router.post("/memory/admin/rebuild-index", response_model=RebuildReport)
async def rebuild_index(request: Request, principal: _AdminMemory) -> RebuildReport:
    """Fully reconstruct every Qdrant collection from PostgreSQL, reconciling by
    point-id == chunk-id set difference (TITAN-BRN-004). Admin-only; runs
    synchronously — laptop-scale collections rebuild in seconds, and the caller
    gets the reconciliation report as the response."""
    state = request.app.state
    qdrant = QdrantIndex(cast(httpx.AsyncClient, state.http))
    embeddings = cast(EmbeddingsClient, state.llm_gateway)
    return await service.rebuild_index(state.engine, qdrant, embeddings)


# --- Knowledge Base write workflow (§10.3, TITAN-BRN-021) --------------------


async def _require_kb(resolver: PermissionResolver, principal: Principal, permission: str) -> None:
    """KB authority is org-wide for humans, project-derived for agents (an
    agent's operator bundle applies only to its bound project, TITAN-BCK-021).
    A proposal/approval is a governed org action, so an agent qualifies when its
    bound project grants the permission — mirroring the memory:write rule."""
    if principal.kind == "agent":
        project_id = await resolver.bound_project()
        granted = await resolver.project_permissions(project_id) if project_id else set()
        if not permission_granted(permission, granted):
            raise forbidden(permission)
    elif not await resolver.has(permission):
        raise forbidden(permission)


@router.post("/kb/proposals", response_model=KbProposalOut, status_code=201)
async def create_kb_proposal(
    request: Request, body: KbProposalIn, principal: _Authenticated
) -> KbProposalOut:
    """Submit a KB proposal (§10.3 step 1): a diff + rationale for a new doc or a
    new version. Never mutates a KB table — it queues a ``pending`` proposal with
    its type-based routing and a semantic near-duplicate conflict check. Requires
    ``kb:propose``. ``proposed_by`` is the authenticated principal, not spoofable
    body input (provenance, TITAN-BRN-021)."""
    resolver = get_resolver(request)
    await _require_kb(resolver, principal, "kb:propose")
    state = request.app.state
    return await service.create_proposal(
        state.engine,
        QdrantIndex(cast(httpx.AsyncClient, state.http)),
        cast(EmbeddingsClient, state.llm_gateway),
        body,
        proposed_by=_actor(principal),
    )


@router.post("/kb/proposals/{proposal_id}/approve", response_model=KbApprovalResult)
async def approve_kb_proposal(
    request: Request, proposal_id: uuid.UUID, principal: _Authenticated
) -> KbApprovalResult:
    """Approve a pending proposal (§10.3 step 3): the new version becomes current,
    is materialised into ``titan_kb`` through the §5.1 pipeline, and KG provenance
    is written. Requires ``kb:approve`` (the Code-Reviewer approval authority);
    protected types (coding_standard/decision_record) additionally require a
    HUMAN approver — the §10.3 human sign-off — enforced in the service."""
    resolver = get_resolver(request)
    await _require_kb(resolver, principal, "kb:approve")
    return await service.approve_proposal(
        request.app.state.engine,
        proposal_id,
        approver=_actor(principal),
        approver_is_human=principal.kind == "user",
    )


@router.get("/kb/documents/{slug}", response_model=KbDocumentDetail)
async def get_kb_document(request: Request, slug: str, principal: _ReadMemory) -> KbDocumentDetail:
    """A KB document with its full version history (§10.2) — older versions stay
    queryable even though only the current one is chunked into titan_kb."""
    detail = await service.get_document_detail(request.app.state.engine, slug)
    if detail is None:
        raise not_found("KB document")
    return detail


def _kb_direct_write_forbidden() -> ProblemException:
    return ProblemException(
        status=403,
        title="Forbidden",
        detail=(
            "The Knowledge Base is append-only through the proposal→approval "
            "workflow (TITAN-BRN-021); POST /kb/proposals then approve. Direct "
            "KB mutation is not permitted."
        ),
        problem_type=problem_type("forbidden"),
    )


@router.post("/kb/documents")
async def create_kb_document_directly(principal: _Authenticated) -> None:
    """Direct KB writes are forbidden — always 403 (TITAN-BRN-021). Present so the
    invariant is explicit and testable, even for an owner (``*``)."""
    raise _kb_direct_write_forbidden()


@router.put("/kb/documents/{slug}")
async def update_kb_document_directly(slug: str, principal: _Authenticated) -> None:
    """Direct KB writes are forbidden — always 403 (TITAN-BRN-021)."""
    raise _kb_direct_write_forbidden()


# --- Knowledge graph traversal (§7.1) ----------------------------------------


@router.post("/memory/kg/query", response_model=KgQueryResult)
async def query_kg(request: Request, body: KgQueryRequest, principal: _ReadMemory) -> KgQueryResult:
    """Typed graph traversal (start nodes, edge-type filter, depth ≤ 3) via a
    recursive CTE (§7.1). Scope-isolated: org + the caller's own project only
    (TITAN-BRN-003)."""
    return await service.kg_query(request.app.state.engine, body)


# --- §9 curation + §11/§12 learning loops ------------------------------------


class CurationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = True
    approve_destructive: bool = False


class TaskReflectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    task_id: uuid.UUID
    agent_id: str = Field(min_length=1, max_length=200)
    context: str = Field(min_length=1, max_length=8000)


class SprintRetroRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    task_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)


class MineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID


class ReflectionStatusOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    has_reflection: bool


async def _require_project_memory_write(
    resolver: PermissionResolver, principal: Principal, project_id: uuid.UUID
) -> None:
    """Project-scoped memory:write: an agent qualifies only in its bound project
    (TITAN-BCK-021), a human via org memory:write — the write_events rule."""
    if principal.kind == "agent":
        granted = await resolver.project_permissions(project_id)
        if not permission_granted("memory:write", granted):
            raise forbidden("memory:write")
    elif not await resolver.has("memory:write"):
        raise forbidden("memory:write")


@router.post("/memory/admin/curation/run", response_model=CurationReport)
async def run_curation_endpoint(
    request: Request, body: CurationRunRequest, principal: _AdminMemory
) -> CurationReport:
    """Trigger one §9 decay/prune/promotion cycle (also the nightly cron). Admin
    only. Dry-run-first by default: the plan is written to the ledger; pass
    ``dry_run=false`` to apply Demote/Archive (and Compact within the approval
    limit, or with ``approve_destructive``). TITAN-BRN-018."""
    return await service.run_curation(
        request.app.state.engine,
        actor_agent=_actor(principal),
        dry_run=body.dry_run,
        approve_destructive=body.approve_destructive,
    )


@router.post("/memory/lessons", response_model=LessonResult, status_code=201)
async def file_lesson_endpoint(
    request: Request, body: LessonIn, principal: _Authenticated
) -> LessonResult:
    """File a structured lesson (§11.2): a protected episodic record + a lesson KG
    node with a DERIVED_FROM edge (TITAN-BRN-019/023). Project-scoped
    ``memory:write``."""
    resolver = get_resolver(request)
    project_id = service.parse_scope(body.scope)
    if project_id is None:
        raise ProblemException(
            status=422,
            title="Invalid request",
            detail="lessons are project-scoped; scope must be 'project:<uuid>'.",
            problem_type=problem_type("validation"),
        )
    await _require_project_memory_write(resolver, principal, project_id)
    return await service.file_lesson(
        request.app.state.engine,
        body,
        source_agent=_actor(principal),
        actor_agent=_actor(principal),
    )


@router.post("/memory/reflections/task", response_model=ReflectionResult, status_code=201)
async def task_reflection_endpoint(
    request: Request, body: TaskReflectionRequest, principal: _Authenticated
) -> ReflectionResult:
    """§12 task reflection: one utility-tier LLM call answers the five-question
    rubric and persists it as an episodic record for the task (TITAN-BRN-025).
    Project-scoped ``memory:write``."""
    resolver = get_resolver(request)
    await _require_project_memory_write(resolver, principal, body.project_id)
    state = request.app.state
    return await service.generate_task_reflection(
        state.engine,
        state.llm_gateway,
        project_id=body.project_id,
        task_id=body.task_id,
        agent_id=body.agent_id,
        context=body.context,
        model_alias=getattr(state, "summary_model_alias", None),
    )


@router.get("/memory/tasks/{task_id}/reflection-status", response_model=ReflectionStatusOut)
async def reflection_status_endpoint(
    request: Request, task_id: uuid.UUID, principal: _ReadMemory
) -> ReflectionStatusOut:
    """The §12 done-block hook exposed for the task state machine (TITAN-BRN-025):
    whether the task has a schema-valid reflection record yet."""
    ok = await service.task_has_reflection(request.app.state.engine, task_id)
    return ReflectionStatusOut(task_id=task_id, has_reflection=ok)


@router.post("/memory/reflections/sprint", response_model=SprintRetroResult, status_code=201)
async def sprint_retro_endpoint(
    request: Request, body: SprintRetroRequest, principal: _Authenticated
) -> SprintRetroResult:
    """§12 sprint retrospective: aggregate a sprint's task reflections into a
    sprint summary record. Project-scoped ``memory:write``."""
    resolver = get_resolver(request)
    await _require_project_memory_write(resolver, principal, body.project_id)
    return await service.sprint_retrospective(
        request.app.state.engine,
        project_id=body.project_id,
        task_ids=body.task_ids,
        actor_agent=_actor(principal),
    )


@router.post("/memory/admin/patterns/mine", response_model=MinerReport)
async def mine_patterns_endpoint(
    request: Request, body: MineRequest, principal: _AdminMemory
) -> MinerReport:
    """§11.1 pattern miner (TITAN-BRN-023): mine novel patterns from a project's
    decisions into project-scope pattern records. Admin only; the run is
    ledger-visible."""
    state = request.app.state
    return await service.mine_patterns(
        state.engine,
        state.llm_gateway,
        QdrantIndex(cast(httpx.AsyncClient, state.http)),
        cast(EmbeddingsClient, state.llm_gateway),
        project_id=body.project_id,
        model_alias=getattr(state, "summary_model_alias", None),
    )


@router.post("/memory/admin/promote", response_model=PromotionResult)
async def promote_endpoint(
    request: Request, body: PromotionRequest, principal: _AdminMemory
) -> PromotionResult:
    """§11.3 project→org promotion on the multi-signal rule (TITAN-BRN-024). Admin
    only — org memory is never filled on a single project's say-so, and agents
    cannot write org scope directly."""
    state = request.app.state
    return await service.promote_record(
        state.engine,
        QdrantIndex(cast(httpx.AsyncClient, state.http)),
        cast(EmbeddingsClient, state.llm_gateway),
        body,
        actor_agent=_actor(principal),
    )


def mount(app: FastAPI) -> None:
    app.include_router(router)
