"""Agent-registry REST endpoints (§4.2), on the Phase-7 conventions framework
and guarded by the Phase-9 Requires dependency. Private to the module; main.py
attaches it via mount().

Agents are an org-level registry (path carries no project), so authorization is
org-scoped: any *:read principal may browse; agent:configure (owner/admin) may
create, edit, pause, or resume.
"""

import hashlib
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response

from titan.agents import service
from titan.agents.service import AgentCreate, AgentDefinition, AgentOut, AgentPatch
from titan.gateway import cache
from titan.gateway.concurrency import require_if_match, set_etag, version_conflict
from titan.gateway.pagination import Page, Pagination, keyset_from
from titan.gateway.rbac import Requires
from titan.identity.service import Principal

router = APIRouter(prefix="/api/v1", tags=["agents"])

_Read = Annotated[Principal, Depends(Requires("agent:read"))]
_Configure = Annotated[Principal, Depends(Requires("agent:configure"))]

_AgentPage = Page[AgentOut]


async def _invalidate_agents(request: Request) -> None:
    """Any agent write bumps the org-wide agents version, invalidating every
    cached (role, status, cursor) variant of the registry list (TITAN-BCK-023)."""
    await cache.bump_version(request.app.state.redis, cache.AGENTS_VERSION_KEY)


@router.get("/agents", response_model=Page[AgentOut])
async def list_agents(
    request: Request,
    response: Response,
    pagination: Pagination,
    principal: _Read,
    role: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> Page[AgentOut] | Response:
    keyset = keyset_from(pagination)

    async def _load() -> _AgentPage:
        async with request.app.state.engine.connect() as conn:
            return await service.list_agents(conn, keyset=keyset, role=role, status=status)

    # Read-through cached; the query fingerprint namespaces each variant, and the
    # shared version counter (bumped on any agent write) invalidates them all.
    # JSON (not a '|'-join) so distinct queries can't collide onto one key — e.g.
    # role="a|b",status="" vs role="a",status="b" would share a key otherwise
    # (Phase-70 review).
    fingerprint = hashlib.sha256(
        json.dumps([role, status, pagination.limit, pagination.position], default=str).encode()
    ).hexdigest()[:16]
    outcome = await cache.cached_get(
        request,
        response,
        resource="agents",
        group=f"agents:{fingerprint}",
        version_key=cache.AGENTS_VERSION_KEY,
        ttl_seconds=request.app.state.settings.cache_agents_ttl_seconds,
        loader=_load,
        model=_AgentPage,
    )
    if outcome.not_modified:
        return Response(status_code=304, headers=dict(response.headers))
    assert outcome.value is not None
    return outcome.value


@router.post("/agents", response_model=AgentOut, status_code=201)
async def create_agent(
    request: Request, body: AgentCreate, response: Response, principal: _Configure
) -> AgentOut:
    async with request.app.state.engine.begin() as conn:
        agent = await service.create_agent(conn, body)
    await _invalidate_agents(request)
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
    await _invalidate_agents(request)
    set_etag(response, updated.version)
    return updated


@router.post("/agents/{agent_id}:pause", response_model=AgentOut)
async def pause_agent(
    request: Request, agent_id: uuid.UUID, response: Response, principal: _Configure
) -> AgentOut:
    async with request.app.state.engine.begin() as conn:
        agent = await service.pause_agent(conn, agent_id, str(principal.sub))
    await _invalidate_agents(request)
    set_etag(response, agent.version)
    return agent


@router.post("/agents/{agent_id}:resume", response_model=AgentOut)
async def resume_agent(
    request: Request, agent_id: uuid.UUID, response: Response, principal: _Configure
) -> AgentOut:
    async with request.app.state.engine.begin() as conn:
        agent = await service.resume_agent(conn, agent_id, str(principal.sub))
    await _invalidate_agents(request)
    set_etag(response, agent.version)
    return agent


# --- Declarative roster (Phase 26; Vol 2 §2.1, TITAN-AGT-008) ---------------
# The *definitions* (role contracts loaded from YAML) alongside the runtime
# *instances* above, on the same agents router — one agents API, not two. Read
# from the in-memory registry built + validated at startup; any `agent:read`
# principal may browse the roster (Agent Status view, TITAN-AGT-008).


@router.get("/agent-definitions", response_model=list[AgentDefinition])
async def list_agent_definitions(request: Request, principal: _Read) -> list[AgentDefinition]:
    return service.list_definitions(request.app.state.agent_registry)


@router.get("/agent-definitions/{agent_id}", response_model=AgentDefinition)
async def get_agent_definition(
    request: Request, agent_id: str, principal: _Read
) -> AgentDefinition:
    return service.get_definition(request.app.state.agent_registry, agent_id)


def mount(app: FastAPI) -> None:
    app.include_router(router)
