"""The exact §4.2 call/result envelopes — persisted, transmitted, returned.

``ToolCallEnvelope`` is what the agent runtime builds from an LLM ``tool_use``
block and hands to the bus; ``ToolResultEnvelope`` is what the bus returns and
what becomes the next turn's ``tool_result``. Field names and shapes match the
Vol 7 §4.2 examples byte-for-byte (``tool_call_id``, ``trace_id``,
``project_id``, ``task_id``, ``agent_id``, ``tool``, ``arguments``,
``requested_at``; ``status``, ``duration_ms``, ``result``, ``stderr_tail``,
``artifacts`` — plus ``code``/``message`` on errors).
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

from titan.db import uuid7
from titan.tools.errors import ToolErrorCode


def _current_trace_id() -> str:
    """The active OTel trace id (32 hex) so audit rows and spans correlate;
    a fresh id when no span is recording (collector-less runs stay linkable
    across the STARTED/finalized audit pair)."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        return trace.format_trace_id(ctx.trace_id)
    return uuid7().hex


class ToolCallEnvelope(BaseModel):
    """A single tool invocation request (§4.2 call envelope)."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    agent_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime


class Artifact(BaseModel):
    """A file/blob a tool produced, referenced (not inlined) in the result."""

    model_config = ConfigDict(extra="forbid")

    kind: str = "file"
    path: str
    size_bytes: int | None = None


class ToolResultEnvelope(BaseModel):
    """The §4.2 result envelope. ``status='error'`` carries the taxonomy
    ``code`` and an LLM-readable ``message``; ``result`` is then empty."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str
    status: Literal["ok", "error"]
    duration_ms: int = 0
    result: dict[str, Any] = Field(default_factory=dict)
    stderr_tail: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
    code: ToolErrorCode | None = None
    message: str | None = None


def new_call(
    tool: str,
    arguments: dict[str, Any],
    *,
    project_id: uuid.UUID,
    agent_id: str,
    task_id: uuid.UUID | None = None,
) -> ToolCallEnvelope:
    """Mint a call envelope for one LLM tool_use block: fresh ``tool_call_id``,
    the current trace id, and an UTC ``requested_at``."""
    return ToolCallEnvelope(
        tool_call_id=f"tc_{uuid7().hex}",
        trace_id=_current_trace_id(),
        project_id=project_id,
        task_id=task_id,
        agent_id=agent_id,
        tool=tool,
        arguments=arguments,
        requested_at=datetime.now(tz=UTC),
    )


__all__ = ["Artifact", "ToolCallEnvelope", "ToolResultEnvelope", "new_call"]
