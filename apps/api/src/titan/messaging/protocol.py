"""The structured message protocol (Vol 2 §6, TITAN-AGT-015).

Every inter-agent communication is one strictly-validated :class:`AgentMessage`
envelope (§6.1) carrying one of exactly eight normative payloads (§6.2). "Strict"
is load-bearing: an unknown ``type`` is rejected, and an unknown or missing
payload field is rejected — ADR-AGT-04's whole point is that agent chatter is
machine-checkable, never free prose. A ninth type or an extra field raises a
:class:`pydantic.ValidationError`, which the caller logs and turns into a 422.

The wire ``type`` is PascalCase (``TaskAssignment``); the ``messages`` table and
the Phase-11 REST surface key on the snake_case ``kind`` (``task_assignment``).
:data:`TYPE_TO_KIND` / :data:`KIND_TO_TYPE` bridge the two so there is one
canonical set of eight, named consistently on each side.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from titan.db import uuid7

logger = structlog.get_logger("titan.messaging.protocol")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- the eight normative types (§6.2) --------------------------------------

MessageType = Literal[
    "TaskAssignment",
    "ProgressReport",
    "ReviewRequest",
    "ArchitectureDiscussion",
    "BugReport",
    "DecisionLog",
    "MeetingSummary",
    "ConflictResolution",
]

Priority = Literal["critical", "high", "normal", "low"]

# Wire PascalCase type ↔ persisted snake_case kind (the messages.kind column).
TYPE_TO_KIND: dict[str, str] = {
    "TaskAssignment": "task_assignment",
    "ProgressReport": "progress_report",
    "ReviewRequest": "review_request",
    "ArchitectureDiscussion": "architecture_discussion",
    "BugReport": "bug_report",
    "DecisionLog": "decision_log",
    "MeetingSummary": "meeting_summary",
    "ConflictResolution": "conflict_resolution",
}
KIND_TO_TYPE: dict[str, str] = {v: k for k, v in TYPE_TO_KIND.items()}


class _Strict(BaseModel):
    """Base for every payload + sub-object: reject unknown fields (§6.1)."""

    model_config = ConfigDict(extra="forbid")


# --- payload sub-objects ---------------------------------------------------


class Blocker(_Strict):
    description: str
    needs: str


class ReviewComment(_Strict):
    location: str
    severity: str
    comment: str
    standard_ref: str | None = None


class Verdict(_Strict):
    decision: Literal["approve", "request_changes", "reject", "veto"]
    comments: list[ReviewComment] = Field(default_factory=list)


class Alternative(_Strict):
    option: str
    rejected_because: str


class ActionItem(_Strict):
    owner: str
    action: str
    due: str | None = None


class Position(_Strict):
    agent: str
    position: str
    evidence: list[str] = Field(default_factory=list)


# --- the eight payloads (§6.2) ---------------------------------------------


class TaskAssignmentPayload(_Strict):
    """Creates/assigns work — the only way work changes hands."""

    title: str
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    story_id: uuid.UUID | None = None
    depends_on: list[uuid.UUID] = Field(default_factory=list)
    estimate_points: int | None = None
    deadline: datetime | None = None


class ProgressReportPayload(_Strict):
    status: Literal["in_progress", "blocked", "completed", "failed"]
    percent_complete: int = Field(ge=0, le=100)
    summary: str
    blockers: list[Blocker] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


class ReviewRequestPayload(_Strict):
    """Asks a gatekeeper for a verdict; the reply reuses ``correlation_id`` and
    fills ``verdict`` (§6.2)."""

    artifact_type: Literal["pull_request", "plan", "architecture", "test_plan", "release"]
    artifact_ref: str
    context_task_id: uuid.UUID | None = None
    checklist: list[str] = Field(default_factory=list)
    verdict: Verdict | None = None


class ArchitectureDiscussionPayload(_Strict):
    thread_id: uuid.UUID | None = None
    question: str
    position: str
    arguments: list[str] = Field(default_factory=list)
    responding_to: uuid.UUID | None = None
    decision_deadline: datetime | None = None


class BugReportPayload(_Strict):
    severity: Literal["critical", "high", "medium", "low"]
    security: bool = False
    summary: str
    # §6.2: BugReport is a "defect with mandatory reproduction" — at least one step.
    reproduction_steps: list[str] = Field(min_length=1)
    expected: str
    actual: str
    suspected_component: str | None = None
    evidence: list[str] = Field(default_factory=list)


class DecisionLogPayload(_Strict):
    decision: str
    context: str
    alternatives: list[Alternative] = Field(default_factory=list)
    trade_offs: str
    authority: str
    reversible: bool


class MeetingSummaryPayload(_Strict):
    ceremony: Literal["sprint_planning", "standup", "post_mortem", "conflict_resolution"]
    participants: list[str] = Field(default_factory=list)
    decisions: list[uuid.UUID] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    summary: str


class ConflictResolutionPayload(_Strict):
    conflict_id: uuid.UUID
    parties: list[str] = Field(default_factory=list)
    subject: str
    positions: list[Position] = Field(default_factory=list)
    round: int = Field(ge=1)
    arbiter: str | None = None
    resolution: str | None = None


Payload = (
    TaskAssignmentPayload
    | ProgressReportPayload
    | ReviewRequestPayload
    | ArchitectureDiscussionPayload
    | BugReportPayload
    | DecisionLogPayload
    | MeetingSummaryPayload
    | ConflictResolutionPayload
)

PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "TaskAssignment": TaskAssignmentPayload,
    "ProgressReport": ProgressReportPayload,
    "ReviewRequest": ReviewRequestPayload,
    "ArchitectureDiscussion": ArchitectureDiscussionPayload,
    "BugReport": BugReportPayload,
    "DecisionLog": DecisionLogPayload,
    "MeetingSummary": MeetingSummaryPayload,
    "ConflictResolution": ConflictResolutionPayload,
}


def payload_model_for(message_type: str) -> type[BaseModel]:
    model = PAYLOAD_MODELS.get(message_type)
    if model is None:
        raise ValueError(f"unknown message type '{message_type}'")
    return model


def validate_payload(message_type: str, payload: dict[str, Any]) -> BaseModel:
    """Validate a raw payload against its type's strict model (raises on an
    unknown field). Used by the bus and by the Phase-11 body-schema registry."""
    return payload_model_for(message_type).model_validate(payload)


# --- the envelope (§6.1) ---------------------------------------------------


class AgentMessage(BaseModel):
    """The single envelope every message shares (§6.1). ``payload`` is kept as a
    dict for lossless persistence/round-trip, but is validated against its
    type's strict model at construction — so an unknown payload field is
    rejected here, not silently stored."""

    model_config = ConfigDict(extra="forbid")

    message_id: uuid.UUID = Field(default_factory=uuid7)
    schema_version: Literal["1.0"] = "1.0"
    type: MessageType
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    thread_id: uuid.UUID | None = None
    correlation_id: uuid.UUID = Field(default_factory=uuid7)
    from_agent: str = Field(min_length=1)
    to_agent: str = Field(min_length=1)
    priority: Priority = "normal"
    requires_ack: bool = False
    # Stamped at construction so the envelope serialized to the outbox/Redis (and
    # thus the copy every consumer hydrates from) always carries a creation time.
    created_at: datetime = Field(default_factory=_utcnow)
    payload: Annotated[dict[str, Any], Field(default_factory=dict)]

    @model_validator(mode="after")
    def _validate_payload(self) -> "AgentMessage":
        # Reject an unknown / malformed payload for this type (§6.1 strict).
        validate_payload(self.type, self.payload)
        return self

    @property
    def kind(self) -> str:
        """The snake_case ``messages.kind`` for this envelope's type."""
        return TYPE_TO_KIND[self.type]

    def typed_payload(self) -> BaseModel:
        """The validated, strongly-typed payload model."""
        return validate_payload(self.type, self.payload)


def parse_agent_message(data: dict[str, Any]) -> AgentMessage:
    """Validate a wire message into an :class:`AgentMessage`, LOGGING any strict
    rejection (unknown type, unknown/missing payload field) before re-raising —
    the "returns a validation error and is logged" half of TITAN-AGT-015.

    The log records only safe metadata (the message type and each error's field
    location + error kind) — never the offending field VALUES, which a raw
    ``ValidationError`` message would embed (TITAN-OPS-016: no message content in
    logs). This is the ingress the agent runtime uses to admit bus traffic.
    """
    try:
        return AgentMessage.model_validate(data)
    except ValidationError as exc:
        logger.warning(
            "agent_message.rejected",
            message_type=data.get("type") if isinstance(data, dict) else None,
            errors=[{"loc": e["loc"], "type": e["type"]} for e in exc.errors()],
        )
        raise


__all__ = [
    "KIND_TO_TYPE",
    "PAYLOAD_MODELS",
    "TYPE_TO_KIND",
    "ActionItem",
    "AgentMessage",
    "Alternative",
    "ArchitectureDiscussionPayload",
    "Blocker",
    "BugReportPayload",
    "ConflictResolutionPayload",
    "DecisionLogPayload",
    "MeetingSummaryPayload",
    "MessageType",
    "Payload",
    "Position",
    "Priority",
    "ProgressReportPayload",
    "ReviewComment",
    "ReviewRequestPayload",
    "TaskAssignmentPayload",
    "Verdict",
    "parse_agent_message",
    "payload_model_for",
    "validate_payload",
]
