"""Public service interface of the `messaging` module (TITAN-BCK-004).

Responsibility: structured inter-agent messages, the decision log, and per-user
notifications.
Owns: messages, decisions, notifications.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""

import json
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7
from titan.events.service import STREAM_EVENTS_DOMAIN, write_event
from titan.gateway.dto import JsonbDict
from titan.gateway.pagination import Keyset, Page
from titan.gateway.problems import ProblemException, problem_type
from titan.messaging import repository as repo
from titan.messaging.bus import (
    ACK_TIMEOUTS_MS,
    RUNTIME_GROUP,
    ack_inbox,
    ack_timeout_ms,
    clear_watchdogs,
    consume_inbox,
    ensure_inbox,
    inbox_stream,
    reclaim_unacked,
    register_watchdog,
)
from titan.messaging.bus import send as send_agent_message
from titan.messaging.protocol import (
    AgentMessage,
    ArchitectureDiscussionPayload,
    BugReportPayload,
    ConflictResolutionPayload,
    DecisionLogPayload,
    MeetingSummaryPayload,
    MessageType,
    Priority,
    ProgressReportPayload,
    ReviewRequestPayload,
    TaskAssignmentPayload,
    Verdict,
    parse_agent_message,
    validate_payload,
)

# --- messages: kinds + the per-kind body-schema hook -----------------------

MessageKind = Literal[
    "task_assignment",
    "progress_report",
    "review_request",
    "architecture_discussion",
    "bug_report",
    "decision_log",
    "meeting_summary",
    "conflict_resolution",
    "human",
]
MESSAGE_KINDS: tuple[str, ...] = (
    "task_assignment",
    "progress_report",
    "review_request",
    "architecture_discussion",
    "bug_report",
    "decision_log",
    "meeting_summary",
    "conflict_resolution",
    "human",
)

#: A body validator raises ValueError on a malformed body; the service maps that
#: to a 422 problem. Vol 2's strict per-kind schemas register here later without
#: touching the router or the service.
BodyValidator = Callable[[dict[str, Any]], None]
_BODY_VALIDATORS: dict[str, BodyValidator] = {}


def register_body_schema(kind: str, validator: BodyValidator) -> None:
    """Register (or override) the body validator for a message kind."""
    _BODY_VALIDATORS[kind] = validator


def _permissive(_body: dict[str, Any]) -> None:
    """MVP default: any JSON object is accepted."""


for _kind in MESSAGE_KINDS:  # permissive default per kind
    register_body_schema(_kind, _permissive)


def validate_message_body(kind: str, body: dict[str, Any]) -> None:
    validator = _BODY_VALIDATORS.get(kind, _permissive)
    try:
        validator(body)
    except ValueError as exc:
        raise ProblemException(
            status=422,
            title="Invalid message body",
            detail=str(exc),
            problem_type=problem_type("validation"),
        ) from exc


# --- DTOs ------------------------------------------------------------------


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MessageKind
    body: dict[str, Any]
    # Omit to start a new conversation (a fresh UUIDv7 thread is minted).
    thread_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    thread_id: uuid.UUID
    kind: str
    sender_agent_id: uuid.UUID | None
    sender_user_id: uuid.UUID | None
    body: JsonbDict
    task_id: uuid.UUID | None
    # §6.1 AgentMessage envelope (null on human/REST messages; set on the bus).
    schema_version: str
    correlation_id: uuid.UUID | None
    from_agent: str | None
    to_agent: str | None
    priority: str
    requires_ack: bool
    created_at: datetime
    version: int


DecisionStatus = Literal["proposed", "accepted", "superseded"]


class DecisionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    context: str = Field(min_length=1)
    decision: str = Field(min_length=1)
    alternatives: str = Field(min_length=1)
    consequences: str = Field(min_length=1)
    status: DecisionStatus = "accepted"
    # The authority that made the decision (§7): decides_alone | arbitration |
    # executive | human_override. Null for a plain ADR that predates the classification.
    authority: str | None = None
    decided_by_agent_id: uuid.UUID | None = None
    supersedes_id: uuid.UUID | None = None


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    context: str
    decision: str
    alternatives: str
    consequences: str
    status: str
    authority: str | None
    decided_by_agent_id: uuid.UUID | None
    supersedes_id: uuid.UUID | None
    created_at: datetime
    version: int


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    category: str
    severity: str
    title: str
    body: str | None
    link: str | None
    read_at: datetime | None
    created_at: datetime
    version: int


def _validation_error(detail: str) -> ProblemException:
    return ProblemException(
        status=422, title="Invalid request", detail=detail, problem_type=problem_type("validation")
    )


# --- messages --------------------------------------------------------------


async def create_message(
    conn: AsyncConnection,
    project_id: uuid.UUID,
    data: MessageCreate,
    *,
    sender_agent_id: uuid.UUID | None,
    sender_user_id: uuid.UUID | None,
    actor: str,
) -> MessageOut:
    """Post a message. The sender is the authenticated principal (never client
    input): a user posts as their user id, an agent as its agent id — which
    satisfies the DB's 'a sender must be present' check without spoofing."""
    if sender_agent_id is None and sender_user_id is None:
        raise _validation_error("A message must have a sender.")
    validate_message_body(data.kind, data.body)
    # A linked task must live in THIS project (mirrors the decisions
    # supersedes_id same-project check) — no cross-project references. Imported
    # lazily: the orchestrator imports messaging.service (for transition
    # messages), so a top-level import here would close a cycle.
    from titan.orchestrator import service as tasks

    if data.task_id is not None and not await tasks.task_in_project(conn, data.task_id, project_id):
        raise _validation_error("task_id does not reference a task in this project.")
    try:
        row = await repo.insert_message(
            conn,
            message_id=uuid7(),
            project_id=project_id,
            thread_id=data.thread_id or uuid7(),
            kind=data.kind,
            sender_agent_id=sender_agent_id,
            sender_user_id=sender_user_id,
            body=json.dumps(data.body),
            task_id=data.task_id,
        )
    except IntegrityError as exc:
        # A bad task_id or sender_agent_id FK is client error, not a 500.
        raise _validation_error("References an unknown task or sender agent.") from exc
    message = MessageOut.model_validate(row)
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {"type": "message.created", "message": message.model_dump(mode="json")},
        actor=actor,
    )
    return message


async def list_messages(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    keyset: Keyset,
    thread_id: uuid.UUID | None,
    kind: str | None,
) -> Page[MessageOut]:
    rows = await repo.list_messages(
        conn, project_id=project_id, keyset=keyset, thread_id=thread_id, kind=kind
    )
    page, cursor = keyset.paginate(rows)
    return Page(items=[MessageOut.model_validate(r) for r in page], next_cursor=cursor)


async def messages_for_project(conn: AsyncConnection, project_id: uuid.UUID) -> list[MessageOut]:
    """The complete durable message log for a project, oldest first — the
    source of truth the chaos-replay reconciles Redis against (TITAN-AGT-016)."""
    rows = await repo.messages_for_project(conn, project_id)
    return [MessageOut.model_validate(r) for r in rows]


async def messages_in_thread(
    conn: AsyncConnection, project_id: uuid.UUID, thread_id: uuid.UUID
) -> list[MessageOut]:
    """One conversation thread's messages, oldest first — the agent runtime's
    turn-hydration source (Vol 2 §8 step 3)."""
    rows = await repo.messages_in_thread(conn, project_id, thread_id)
    return [MessageOut.model_validate(r) for r in rows]


async def message_exists(conn: AsyncConnection, message_id: uuid.UUID) -> bool:
    """Whether a message id is already persisted — the runtime's idempotent
    replay guard so a reprocessed turn re-emits nothing (TITAN-AGT-002/016)."""
    return await repo.message_exists(conn, message_id)


async def get_message(conn: AsyncConnection, message_id: uuid.UUID) -> MessageOut | None:
    """One message by id, or None — the runtime reads a replayed turn's emitted
    message to recover its true outcome (escalation vs. completion)."""
    row = await repo.get_message(conn, message_id)
    return MessageOut.model_validate(row) if row is not None else None


class ExpiredDiscussion(BaseModel):
    """One ArchitectureDiscussion thread past its decision deadline with no
    DecisionLog — the watchdog auto-closes or escalates it (TITAN-AGT-014)."""

    project_id: uuid.UUID
    thread_id: uuid.UUID


async def expired_discussion_threads(
    conn: AsyncConnection, *, now: datetime
) -> list[ExpiredDiscussion]:
    """Discussion threads whose ``decision_deadline`` passed without a DecisionLog
    message in the thread (§5.4) — the watchdog's deadline-enforcement source."""
    rows = await repo.expired_discussion_threads(conn, now=now)
    return [ExpiredDiscussion(project_id=r.project_id, thread_id=r.thread_id) for r in rows]


# --- decisions -------------------------------------------------------------


async def create_decision(
    conn: AsyncConnection, project_id: uuid.UUID, data: DecisionCreate, actor: str
) -> DecisionOut:
    """Record an ADR. If it supersedes another, that prior decision's status is
    DERIVED as ``superseded`` (this new row points back at it) — never mutated:
    the decisions table is append-only at the DB layer (TITAN-AGT-019). The
    supersede target must live in this project."""
    if data.supersedes_id is not None:
        prior = await repo.get_decision(conn, data.supersedes_id)
        if prior is None or prior.project_id != project_id:
            raise _validation_error("supersedes_id does not reference a decision in this project.")
    try:
        row = await repo.insert_decision(
            conn,
            decision_id=uuid7(),
            project_id=project_id,
            title=data.title,
            context=data.context,
            decision=data.decision,
            alternatives=data.alternatives,
            consequences=data.consequences,
            status=data.status,
            authority=data.authority,
            decided_by_agent_id=data.decided_by_agent_id,
            supersedes_id=data.supersedes_id,
        )
    except IntegrityError as exc:
        raise _validation_error("References an unknown agent.") from exc
    decision = DecisionOut.model_validate(row)
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {"type": "decision.recorded", "decision": decision.model_dump(mode="json")},
        actor=actor,
    )
    return decision


async def list_decisions(
    conn: AsyncConnection, *, project_id: uuid.UUID, keyset: Keyset
) -> Page[DecisionOut]:
    rows = await repo.list_decisions(conn, project_id=project_id, keyset=keyset)
    page, cursor = keyset.paginate(rows)
    return Page(items=[DecisionOut.model_validate(r) for r in page], next_cursor=cursor)


# --- notifications ---------------------------------------------------------


async def create_notification(
    conn: AsyncConnection,
    *,
    user_id: uuid.UUID,
    category: str,
    severity: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    actor: str = "system",
) -> NotificationOut:
    """Persist a notification for a user (TITAN-FRT-038). The seam other modules
    (approval gates, run failures, …) call to raise the bell badge."""
    row = await repo.insert_notification(
        conn,
        notification_id=uuid7(),
        user_id=user_id,
        category=category,
        severity=severity,
        title=title,
        body=body,
        link=link,
    )
    notification = NotificationOut.model_validate(row)
    # §5.2 notifications:{userId} — user-scoped WS delivery via the outbox path.
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {
            "type": "notification.created",
            "user_id": str(user_id),
            "notification": notification.model_dump(mode="json"),
        },
        actor=actor,
    )
    return notification


async def list_notifications(
    conn: AsyncConnection, *, user_id: uuid.UUID, keyset: Keyset, unread_only: bool
) -> Page[NotificationOut]:
    rows = await repo.list_notifications(
        conn, user_id=user_id, keyset=keyset, unread_only=unread_only
    )
    page, cursor = keyset.paginate(rows)
    return Page(items=[NotificationOut.model_validate(r) for r in page], next_cursor=cursor)


async def mark_notification_read(
    conn: AsyncConnection, notification_id: uuid.UUID, user_id: uuid.UUID
) -> NotificationOut:
    """Mark the caller's own notification read (idempotent). A missing or
    not-owned id is 404 — a user never learns another user's notification ids."""
    row = await repo.mark_notification_read(conn, notification_id, user_id)
    if row is None:
        raise ProblemException(
            status=404,
            title="Not found",
            detail="No such notification.",
            problem_type=problem_type("not-found"),
        )
    return NotificationOut.model_validate(row)


async def mark_all_notifications_read(conn: AsyncConnection, user_id: uuid.UUID) -> int:
    return await repo.mark_all_notifications_read(conn, user_id)


__all__ = [
    "ACK_TIMEOUTS_MS",
    "MESSAGE_KINDS",
    "RUNTIME_GROUP",
    "AgentMessage",
    "ArchitectureDiscussionPayload",
    "BugReportPayload",
    "ConflictResolutionPayload",
    "DecisionCreate",
    "DecisionLogPayload",
    "DecisionOut",
    "ExpiredDiscussion",
    "MeetingSummaryPayload",
    "MessageCreate",
    "MessageKind",
    "MessageOut",
    "MessageType",
    "NotificationOut",
    "Priority",
    "ProgressReportPayload",
    "ReviewRequestPayload",
    "TaskAssignmentPayload",
    "Verdict",
    "ack_inbox",
    "ack_timeout_ms",
    "clear_watchdogs",
    "consume_inbox",
    "create_decision",
    "create_message",
    "create_notification",
    "ensure_inbox",
    "expired_discussion_threads",
    "get_message",
    "inbox_stream",
    "list_decisions",
    "list_messages",
    "list_notifications",
    "mark_all_notifications_read",
    "mark_notification_read",
    "message_exists",
    "messages_for_project",
    "messages_in_thread",
    "parse_agent_message",
    "reclaim_unacked",
    "register_body_schema",
    "register_watchdog",
    "send_agent_message",
    "validate_message_body",
    "validate_payload",
]
