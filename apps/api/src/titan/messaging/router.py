"""Messaging REST endpoints (§4.2): project messages, the decision log, and
per-user notifications. On the Phase-7 framework, guarded by Phase-9 Requires.
Private to the module; main.py attaches it via mount().

Messages/decisions are project-scoped. Notifications are user-scoped: a caller
only ever sees and mutates their OWN rows (filtered by principal.sub), so
RequiresAuth plus that filter is the whole authorization story.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from pydantic import BaseModel

from titan.gateway.pagination import Page, Pagination, keyset_from
from titan.gateway.rbac import Requires, RequiresAuth
from titan.identity.service import Principal
from titan.messaging import service
from titan.messaging.service import (
    DecisionCreate,
    DecisionOut,
    MessageCreate,
    MessageOut,
    NotificationOut,
)
from titan.projects import service as projects

router = APIRouter(prefix="/api/v1", tags=["messaging"])

_ReadProject = Annotated[Principal, Depends(Requires("message:read", project_param="project_id"))]
_WriteMessage = Annotated[Principal, Depends(Requires("message:write", project_param="project_id"))]
_ReadDecisions = Annotated[
    Principal, Depends(Requires("decision:read", project_param="project_id"))
]
_WriteDecision = Annotated[
    Principal, Depends(Requires("decision:write", project_param="project_id"))
]
_Authenticated = Annotated[Principal, Depends(RequiresAuth())]


class MarkAllReadResult(BaseModel):
    marked_read: int


def _sender(principal: Principal) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """(sender_agent_id, sender_user_id) derived from the authenticated caller."""
    if principal.kind == "agent":
        return principal.agent_id, None
    return None, principal.sub


# --- messages --------------------------------------------------------------


@router.get("/projects/{project_id}/messages", response_model=Page[MessageOut])
async def list_messages(
    request: Request,
    project_id: uuid.UUID,
    pagination: Pagination,
    principal: _ReadProject,
    thread_id: Annotated[uuid.UUID | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    task_id: Annotated[uuid.UUID | None, Query()] = None,
    sender_agent_id: Annotated[uuid.UUID | None, Query()] = None,
    from_agent: Annotated[str | None, Query()] = None,
) -> Page[MessageOut]:
    """List a project's messages. ``task_id`` narrows to one task's thread — every
    state change writes a transition message here (``orchestrator.service.
    _write_transition_message``), so this doubles as the task's event history.

    **The two sender filters are not interchangeable, and which one you want is
    almost always ``from_agent``.** A message's sender is recorded in one of
    three mutually exclusive ways:

    - ``sender_agent_id`` — set ONLY by an actor-driven transition, which passes
      the acting agent's id straight through. Narrow but precisely attributed.
    - ``from_agent`` — a ROLE string (the roster id, e.g. ``eng_backend``). This
      is how the agent bus records every message an agent actually emits during a
      turn: ``bus.send`` writes ``sender_agent_id=None`` explicitly, because
      "bus messages identify their sender by role". So agent-to-agent traffic is
      attributed by ROLE, not by agent id — and two agents sharing a role are
      indistinguishable here. That is the system's own attribution model, not a
      gap this filter can close.
    - ``sender_user_id`` — a human. Neither agent filter ever matches one.

    Two further caveats. The log is HOLEY: a transition whose ``message_kind`` is
    None (the scheduler's backlog->ready) writes nothing. And it is LOSSY: many
    actions collapse onto one kind (start/block/merge/fail/... all become
    ``progress_report``), so what happened is ``body.action``, never ``kind``."""
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.list_messages(
            conn,
            project_id=project_id,
            keyset=keyset,
            thread_id=thread_id,
            kind=kind,
            task_id=task_id,
            sender_agent_id=sender_agent_id,
            from_agent=from_agent,
        )


@router.post("/projects/{project_id}/messages", response_model=MessageOut, status_code=201)
async def create_message(
    request: Request,
    project_id: uuid.UUID,
    body: MessageCreate,
    principal: _WriteMessage,
) -> MessageOut:
    sender_agent_id, sender_user_id = _sender(principal)
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)  # 404 before FK insert
        return await service.create_message(
            conn,
            project_id,
            body,
            sender_agent_id=sender_agent_id,
            sender_user_id=sender_user_id,
            actor=str(principal.sub),
        )


# --- decisions -------------------------------------------------------------


@router.get("/projects/{project_id}/decisions", response_model=Page[DecisionOut])
async def list_decisions(
    request: Request, project_id: uuid.UUID, pagination: Pagination, principal: _ReadDecisions
) -> Page[DecisionOut]:
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        await projects.get_project(conn, project_id)
        return await service.list_decisions(conn, project_id=project_id, keyset=keyset)


@router.post("/projects/{project_id}/decisions", response_model=DecisionOut, status_code=201)
async def create_decision(
    request: Request, project_id: uuid.UUID, body: DecisionCreate, principal: _WriteDecision
) -> DecisionOut:
    async with request.app.state.engine.begin() as conn:
        await projects.get_project(conn, project_id)
        return await service.create_decision(conn, project_id, body, str(principal.sub))


# --- notifications (user-scoped) -------------------------------------------


@router.get("/notifications", response_model=Page[NotificationOut])
async def list_notifications(
    request: Request,
    pagination: Pagination,
    principal: _Authenticated,
    unread: Annotated[bool, Query()] = False,
) -> Page[NotificationOut]:
    keyset = keyset_from(pagination)
    async with request.app.state.engine.connect() as conn:
        return await service.list_notifications(
            conn, user_id=principal.sub, keyset=keyset, unread_only=unread
        )


@router.post("/notifications/{notification_id}:mark-read", response_model=NotificationOut)
async def mark_read(
    request: Request, notification_id: uuid.UUID, principal: _Authenticated
) -> NotificationOut:
    async with request.app.state.engine.begin() as conn:
        return await service.mark_notification_read(conn, notification_id, principal.sub)


@router.post("/notifications:mark-all-read", response_model=MarkAllReadResult)
async def mark_all_read(request: Request, principal: _Authenticated) -> MarkAllReadResult:
    async with request.app.state.engine.begin() as conn:
        count = await service.mark_all_notifications_read(conn, principal.sub)
    return MarkAllReadResult(marked_read=count)


def mount(app: FastAPI) -> None:
    app.include_router(router)
