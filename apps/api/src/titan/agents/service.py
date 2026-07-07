"""Public service interface of the `agents` module (TITAN-BCK-004).

Responsibility: agent registry: role, config, model binding, status.
Owns: agents.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.agents import repository as repo
from titan.agents.definition import (
    AgentDefinition,
    Authority,
    Escalation,
    FailurePolicy,
    MemoryScopes,
    ModelPolicy,
)
from titan.agents.loader import RosterError, load_roster_cached
from titan.agents.registry import AgentRegistry
from titan.db import uuid7
from titan.events.service import STREAM_EVENTS_DOMAIN, write_event
from titan.gateway.dto import JsonbDict, RejectNullFields
from titan.gateway.pagination import Keyset, Page
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import not_found
from titan.settings import Settings

# The 14-role roster (Vol 2; §6 stores role as free text, the API constrains it).
AgentRole = Literal[
    "ceo",
    "pm",
    "project_manager",
    "architect",
    "backend",
    "frontend",
    "db",
    "devops",
    "qa",
    "security",
    "reviewer",
    "docs",
    "research",
    "memory",
]
AgentStatus = Literal["idle", "busy", "paused", "error"]


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: AgentRole
    name: str = Field(min_length=1, max_length=200)
    model_binding: dict[str, Any]
    config: dict[str, Any] = Field(default_factory=dict)
    # NULL = org-level template; a project-bound agent names its project.
    project_id: uuid.UUID | None = None


class AgentPatch(RejectNullFields):
    # No `role` (an agent's role defines it — immutable), no `status` (moves via
    # :pause/:resume and the scheduler), no `current_task_id` (assignment is the
    # orchestrator's job). Every patchable field maps to a NOT NULL column.
    model_config = ConfigDict(extra="forbid")
    non_nullable = frozenset({"name", "model_binding", "config"})

    name: str | None = Field(default=None, min_length=1, max_length=200)
    model_binding: dict[str, Any] | None = None
    config: dict[str, Any] | None = None


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID | None
    role: str
    name: str
    status: str
    model_binding: JsonbDict
    config: JsonbDict
    current_task_id: uuid.UUID | None
    created_at: datetime
    version: int


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type=problem_type("conflict")
    )


async def _emit_status(conn: AsyncConnection, agent: AgentOut, actor: str) -> None:
    # §5 WebSocket taxonomy: agent state changes surface as `agent.status`.
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {
            "type": "agent.status",
            "agent_id": str(agent.id),
            "state": agent.status,
            "current_task_id": str(agent.current_task_id) if agent.current_task_id else None,
        },
        actor=actor,
    )


async def agent_exists(conn: AsyncConnection, agent_id: uuid.UUID) -> bool:
    """Existence probe for other modules (e.g. runs validating agent_id)."""
    return await repo.get_agent(conn, agent_id) is not None


async def agent_in_project(
    conn: AsyncConnection, agent_id: uuid.UUID, project_id: uuid.UUID
) -> bool:
    """True if the agent exists AND may act in ``project_id`` — its own project,
    or an org-level template (project_id NULL). The cross-module check the
    orchestrator uses before assigning/reviewing a task with an agent, so a
    non-existent or wrong-project actor_agent_id is a 4xx, not a 500 FK error."""
    row = await repo.get_agent(conn, agent_id)
    return row is not None and (row.project_id is None or row.project_id == project_id)


async def create_agent(conn: AsyncConnection, data: AgentCreate) -> AgentOut:
    try:
        row = await repo.insert_agent(
            conn,
            agent_id=uuid7(),
            project_id=data.project_id,
            role=data.role,
            name=data.name,
            model_binding=json.dumps(data.model_binding),
            config=json.dumps(data.config),
        )
    except IntegrityError as exc:
        # A bad project_id FK is client error (422), not a 500 — matches the
        # create_run / create_message / create_decision convention.
        raise ProblemException(
            status=422,
            title="Invalid agent",
            detail="References an unknown project.",
            problem_type=problem_type("validation"),
        ) from exc
    return AgentOut.model_validate(row)


async def get_agent(conn: AsyncConnection, agent_id: uuid.UUID) -> AgentOut:
    row = await repo.get_agent(conn, agent_id)
    if row is None:
        raise not_found("agent")
    return AgentOut.model_validate(row)


async def list_agents(
    conn: AsyncConnection, *, keyset: Keyset, role: str | None, status: str | None
) -> Page[AgentOut]:
    rows = await repo.list_agents(conn, keyset=keyset, role=role, status=status)
    page, cursor = keyset.paginate(rows)
    return Page(items=[AgentOut.model_validate(r) for r in page], next_cursor=cursor)


async def update_agent(
    conn: AsyncConnection, agent_id: uuid.UUID, patch: AgentPatch, expected_version: int
) -> AgentOut | None:
    fields: dict[str, Any] = {}
    for key, value in patch.model_dump(exclude_unset=True).items():
        fields[key] = json.dumps(value) if key in ("model_binding", "config") else value
    row = await repo.update_agent(conn, agent_id, expected_version, fields)
    return AgentOut.model_validate(row) if row is not None else None


async def pause_agent(conn: AsyncConnection, agent_id: uuid.UUID, actor: str) -> AgentOut:
    """idle|busy|error → paused; already-paused is a 409, missing is a 404."""
    row = await repo.transition_status(
        conn, agent_id, target="paused", allowed_from=("idle", "busy", "error")
    )
    if row is None:
        if await repo.get_agent(conn, agent_id) is None:
            raise not_found("agent")
        raise _conflict("Agent is already paused.")
    agent = AgentOut.model_validate(row)
    await _emit_status(conn, agent, actor)
    return agent


async def resume_agent(conn: AsyncConnection, agent_id: uuid.UUID, actor: str) -> AgentOut:
    """paused → idle; resuming a non-paused agent is a 409, missing is a 404."""
    row = await repo.transition_status(conn, agent_id, target="idle", allowed_from=("paused",))
    if row is None:
        if await repo.get_agent(conn, agent_id) is None:
            raise not_found("agent")
        raise _conflict("Agent is not paused.")
    agent = AgentOut.model_validate(row)
    await _emit_status(conn, agent, actor)
    return agent


# ---------------------------------------------------------------------------
# Declarative agent roster (Phase 26; Vol 2 §2.1, TITAN-AGT-001/006/008).
#
# The `Agent` rows above are the *runtime instances* (per-project, with live
# status); the `AgentDefinition` roster below is the *role contract* loaded from
# YAML. Both are owned by this module and cross the boundary only through this
# service — the loader/registry stay private, as do the ORM models.
# ---------------------------------------------------------------------------


def load_roster(settings: Settings) -> AgentRegistry:
    """Load + validate the declarative roster from the configured YAML dir.

    Raises :class:`RosterError` on a malformed or incomplete roster — the
    lifespan lets that propagate so a bad roster refuses to boot (TITAN-AGT-001).
    """
    return load_roster_cached(settings.agents_config_dir)


def list_definitions(registry: AgentRegistry) -> list[AgentDefinition]:
    """Every role contract, tier-then-id ordered (the dashboard roster view)."""
    return list(registry.all())


def get_definition(registry: AgentRegistry, agent_id: str) -> AgentDefinition:
    """One role contract by agent_id; 404 if the roster has no such role."""
    definition = registry.get(agent_id)
    if definition is None:
        raise not_found("agent definition")
    return definition


__all__ = [
    "AgentCreate",
    "AgentDefinition",
    "AgentOut",
    "AgentPatch",
    "AgentRegistry",
    "AgentRole",
    "AgentStatus",
    "Authority",
    "Escalation",
    "FailurePolicy",
    "MemoryScopes",
    "ModelPolicy",
    "RosterError",
    "agent_exists",
    "agent_in_project",
    "create_agent",
    "get_agent",
    "get_definition",
    "list_agents",
    "list_definitions",
    "load_roster",
    "pause_agent",
    "resume_agent",
    "update_agent",
]
