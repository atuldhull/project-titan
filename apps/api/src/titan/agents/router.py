"""Agent-registry REST endpoints (§4.2), on the Phase-7 conventions framework
and guarded by the Phase-9 Requires dependency. Private to the module; main.py
attaches it via mount().

Agents are an org-level registry (path carries no project), so authorization is
org-scoped: any *:read principal may browse; agent:configure (owner/admin) may
create, edit, pause, or resume.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response

from titan.agents import service
from titan.agents.service import AgentCreate, AgentOut, AgentPatch
from titan.gateway.concurrency import require_if_match, set_etag, version_conflict
from titan.gateway.pagination import Page, Pagination, keyset_from
from titan.gateway.rbac import Requires
from titan.identity.service import Principal

router = APIRouter(prefix="/api/v1", tags=["agents"])

_Read = Annotated[Principal, Depends(Requires("agent:read"))]
_Configure = Annotated[Principal, Depends(Requires("agent:configure"))]


@router.get("/agents", response_model=Page[AgentOut])
async def list_agents(
    request: Request,
    pagination: Pagination,
    principal: _Read,
    role: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> Page[AgentOut]:
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        return await service.list_agents(conn, keyset=keyset, role=role, status=status)


@router.post("/agents", response_model=AgentOut, status_code=201)
async def create_agent(
    request: Request, body: AgentCreate, response: Response, principal: _Configure
) -> AgentOut:
    async with request.app.state.engine.begin() as conn:
        agent = await service.create_agent(conn, body)
    set_etag(response, agent.version)
    return agent


@router.get("/agents/{agent_id}", response_model=AgentOut)
async def get_agent(
    request: Request, agent_id: uuid.UUID, response: Response, principal: _Read
) -> AgentOut:
    async with request.app.state.engine.connect() as conn:
        agent = await service.get_agent(conn, agent_id)
    set_etag(response, agent.version)
    return agent


@router.patch("/agents/{agent_id}", response_model=AgentOut)
async def update_agent(
    request: Request,
    agent_id: uuid.UUID,
    body: AgentPatch,
    response: Response,
    principal: _Configure,
) -> AgentOut:
    async with request.app.state.engine.begin() as conn:
        current = await service.get_agent(conn, agent_id)
        require_if_match(request, current.version)
        updated = await service.update_agent(conn, agent_id, body, current.version)
    if updated is None:
        raise version_conflict()
    set_etag(response, updated.version)
    return updated


@router.post("/agents/{agent_id}:pause", response_model=AgentOut)
async def pause_agent(
    request: Request, agent_id: uuid.UUID, response: Response, principal: _Configure
) -> AgentOut:
    async with request.app.state.engine.begin() as conn:
        agent = await service.pause_agent(conn, agent_id, str(principal.sub))
    set_etag(response, agent.version)
    return agent


@router.post("/agents/{agent_id}:resume", response_model=AgentOut)
async def resume_agent(
    request: Request, agent_id: uuid.UUID, response: Response, principal: _Configure
) -> AgentOut:
    async with request.app.state.engine.begin() as conn:
        agent = await service.resume_agent(conn, agent_id, str(principal.sub))
    set_etag(response, agent.version)
    return agent


def mount(app: FastAPI) -> None:
    app.include_router(router)
