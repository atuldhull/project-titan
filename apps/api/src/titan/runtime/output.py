"""The agent turn's structured output (Vol 2 §8 step 4, §2.2, TITAN-AGT-023).

An agent never returns free prose to the bus (§2.2): its LLM turn ends in a strict
JSON :class:`AgentTurnOutput` — the messages it wants to emit (each a validated
§6.2 payload) plus an optional working note. The runtime validates this with
Pydantic (§8 step 4); invalid output is NEVER silently accepted — it drives the
self-critique retry rung (§2.2 rung 2 / TITAN-AGT-023): the failure detail is fed
back to the model and the turn is re-invoked.

:func:`parse_turn_output` raises :class:`TurnOutputError` carrying a
model-readable failure detail (used verbatim as retry feedback — that is model
input, not a log). Logs and spans get only the counts, never the content.
"""

import json
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from titan.messaging.service import MessageType, Priority, validate_payload


class TurnOutputError(ValueError):
    """Raised when the model's turn output is not a valid ``AgentTurnOutput``.

    Its ``str`` is a compact, content-light description of what was wrong,
    suitable to append to the conversation as self-critique feedback.
    """


class OutboundMessage(BaseModel):
    """One §6.2 message the agent wants to emit this turn. ``payload`` is
    validated against its ``type``'s strict payload model — an unknown or missing
    field is rejected here, exactly as on the bus."""

    model_config = ConfigDict(extra="forbid")

    type: MessageType
    to_agent: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: Priority = "normal"
    requires_ack: bool = False
    # An agent may target a specific task; None inherits the triggering task.
    # Typed as a real UUID so a malformed model-supplied value is rejected HERE
    # (→ self-critique retry, TITAN-AGT-023), never as a crash inside the emit
    # transaction that would misroute the turn to the infra-error/DLQ path.
    task_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _validate_payload(self) -> "OutboundMessage":
        validate_payload(self.type, self.payload)
        return self


class AgentTurnOutput(BaseModel):
    """The complete, strict result of one agent turn (§8 step 4)."""

    model_config = ConfigDict(extra="forbid")

    # A turn emits AT LEAST ONE structured message (§2.2: agents always respond on
    # the bus, never with silence or free prose). Requiring ≥1 also makes the
    # turn's first emitted message a total completion marker, so a completed turn
    # is always detectable and idempotent on crash-replay (never re-runs its tools
    # — see titan.runtime.turn._already_emitted). An empty output is rejected here
    # and drives the self-critique retry.
    messages: tuple[OutboundMessage, ...] = Field(min_length=1)
    # An optional short working note the agent wants persisted to short_term
    # memory (the `memory.write_note` grant); the memory phases consume it.
    note: str | None = None


def _summarize_errors(exc: ValidationError) -> str:
    """A compact, content-light rendering of validation failures for the model.

    Includes the field locations and error types (which guide the model to a
    correct retry) but not the full offending values verbatim, keeping the
    feedback focused and bounded."""
    parts = []
    for err in exc.errors()[:8]:
        loc = "/".join(str(p) for p in err["loc"]) or "(root)"
        parts.append(f"at '{loc}': {err['msg']}")
    more = len(exc.errors()) - 8
    if more > 0:
        parts.append(f"(+{more} more)")
    return "; ".join(parts)


def parse_turn_output(text: str | None) -> AgentTurnOutput:
    """Validate the model's final text into an :class:`AgentTurnOutput`.

    Raises :class:`TurnOutputError` (never a bare ``ValidationError``/JSON error)
    so the caller has one exception type to catch when driving the self-critique
    retry, with a message safe to echo back to the model.
    """
    if text is None or not text.strip():
        raise TurnOutputError("empty response — expected a JSON AgentTurnOutput object")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TurnOutputError(
            f"response was not valid JSON: {exc.msg} at position {exc.pos}"
        ) from exc
    if not isinstance(data, dict):
        raise TurnOutputError("response JSON must be an object with a 'messages' array")
    try:
        return AgentTurnOutput.model_validate(data)
    except ValidationError as exc:
        raise TurnOutputError(_summarize_errors(exc)) from exc


__all__ = ["AgentTurnOutput", "OutboundMessage", "TurnOutputError", "parse_turn_output"]
