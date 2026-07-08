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
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request

from titan.gateway.rbac import Requires, not_found
from titan.identity.service import Principal
from titan.memory import service
from titan.memory.service import MemoryEventBatch, MemoryRecordDetail, MemoryWriteResult

router = APIRouter(prefix="/api/v1", tags=["memory"])

_WriteMemory = Annotated[Principal, Depends(Requires("memory:write"))]
_ReadMemory = Annotated[Principal, Depends(Requires("memory:read"))]


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
    principal: _WriteMemory,
) -> MemoryWriteResult:
    """Write a batch of Memory Events in ONE transaction — a failure anywhere
    rolls back the whole batch (§5.1). ``source_agent`` records WHICH agent role
    authored the memory (application data); the ledger's actor is the
    authenticated principal, never spoofable body input (TITAN-BRN-005)."""
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


def mount(app: FastAPI) -> None:
    app.include_router(router)
