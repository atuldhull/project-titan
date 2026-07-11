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

from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import (
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
    EmbeddingsClient,
    MemoryEventBatch,
    MemoryMetricsOut,
    MemoryPacketOut,
    MemoryRecordDetail,
    MemoryWriteResult,
    PacketRequest,
    QdrantIndex,
    RebuildReport,
    Reranker,
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


def mount(app: FastAPI) -> None:
    app.include_router(router)
