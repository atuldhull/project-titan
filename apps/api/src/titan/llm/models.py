"""Canonical, provider-neutral LLM gateway schema (Vol 7 §8.1, TITAN-INT-060/061).

This is the ONE contract every adapter translates to and from. Agents and the
rest of the platform speak only these Pydantic DTOs; nothing here mentions a
provider. Anthropic/Ollama/OpenAI/Google quirks are confined to the adapters.

Messages are provider-neutral (system/user/assistant/tool roles; content parts
for text, image, tool_use, tool_result). Tool definitions are canonical JSON
Schema (§4.1 — identical to what the provider tool-use APIs consume). Streaming
is normalized to ONE internal delta-event shape (:class:`StreamDelta`).
"""

import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_use", "content_filter", "error"]

# ``model_alias``/``model_id`` collide with Pydantic's protected ``model_``
# namespace; the gateway owns these field names, so we opt out of the guard.
_ALLOW_MODEL_FIELDS = ConfigDict(protected_namespaces=(), extra="forbid")


# --- content parts ---------------------------------------------------------


class TextPart(BaseModel):
    """A run of assistant/user text."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["text"] = "text"
    text: str


class ImageSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_type: str
    data: str  # base64-encoded bytes


class ImagePart(BaseModel):
    """An inline image (base64). Multimodal inputs (§8.1)."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["image"] = "image"
    source: ImageSource


class ToolUsePart(BaseModel):
    """A model-requested tool call. Multiple in one assistant message express
    PARALLEL tool calls (TITAN-INT-061)."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultPart(BaseModel):
    """The result of a tool call, carried on a ``tool``-role message. The
    ``tool_use_id`` ties it back to the originating :class:`ToolUsePart`."""

    model_config = ConfigDict(extra="forbid")
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


ContentPart = Annotated[
    TextPart | ImagePart | ToolUsePart | ToolResultPart,
    Field(discriminator="type"),
]


class Message(BaseModel):
    """A provider-neutral conversation turn. ``content`` accepts a bare string
    (normalized to a single text part) or an explicit list of parts."""

    model_config = ConfigDict(extra="forbid")
    role: Role
    content: str | list[ContentPart]

    def parts(self) -> list[ContentPart]:
        """Content as an explicit part list (bare strings become one text part)."""
        if isinstance(self.content, str):
            return [TextPart(text=self.content)]
        return list(self.content)


# --- tools -----------------------------------------------------------------


class ToolDef(BaseModel):
    """A tool the model may call, defined by JSON Schema (draft 2020-12, §4.1)."""

    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolChoice(BaseModel):
    """How the model may use tools. ``mode='tool'`` forces ``name``."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "required", "none", "tool"] = "auto"
    name: str | None = None

    @model_validator(mode="after")
    def _require_name_for_tool(self) -> "ToolChoice":
        # Rejecting mode='tool' without a name here is a 422 to the caller, not a
        # provider 400 buried in an UpstreamProviderError (adapters would silently
        # drop the choice otherwise).
        if self.mode == "tool" and not self.name:
            raise ValueError("tool_choice.mode='tool' requires a tool name")
        return self


# --- request ---------------------------------------------------------------


class RequestMetadata(BaseModel):
    """Attribution for routing, budgets, and audit (§8.1). ``agent_id`` is the
    routing key consulted by ``routing.by_agent`` (e.g. ``agent.ceo``)."""

    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None


class ChatRequest(BaseModel):
    """The canonical chat request (§8.1). ``model_alias`` is optional: when
    omitted the gateway routes by ``metadata.agent_id`` then ``routing.default``."""

    model_config = _ALLOW_MODEL_FIELDS
    model_alias: str | None = None
    messages: list[Message] = Field(min_length=1)
    tools: list[ToolDef] | None = None
    tool_choice: ToolChoice | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int = Field(gt=0)
    stream: bool = False
    metadata: RequestMetadata = Field(default_factory=RequestMetadata)


# --- response --------------------------------------------------------------


class Usage(BaseModel):
    """Token accounting (§8.4). ``cached_tokens`` is the prompt-cache read count
    where the provider reports it, else 0."""

    model_config = ConfigDict(extra="forbid")
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class ToolCall(BaseModel):
    """A normalized tool call from the model response. ``arguments`` is decoded
    JSON regardless of how the provider transported it."""

    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    """The canonical chat response (§8.1). ``provider``/``model_id`` reflect what
    actually served the call so fallback substitutions are attributable, and
    ``degraded_model`` flags a result served by a fallback (Vol 9 §10) so later
    review logic can tighten. ``requested_alias`` is what routing first chose."""

    model_config = _ALLOW_MODEL_FIELDS
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: FinishReason
    usage: Usage
    provider: str
    model_id: str
    cost_usd: Decimal
    latency_ms: int
    requested_alias: str | None = None
    served_alias: str | None = None
    degraded_model: bool = False


# --- streaming (one normalized delta shape for every provider) -------------


class StreamDelta(BaseModel):
    """One internal streaming event, provider-agnostic (§8.1). Adapters emit a
    sequence of these; the dashboard WebSocket re-broadcast (Vol 4) consumes the
    same shape.

    - ``text``: an incremental assistant-text chunk.
    - ``tool_use_start``: a tool call began (``tool_call_id``, ``tool_name``).
    - ``tool_use_delta``: a partial-JSON fragment of a call's arguments.
    - ``usage``: token accounting became available.
    - ``done``: the turn finished (carries ``finish_reason`` and final ``usage``).
    """

    model_config = ConfigDict(extra="forbid")
    type: Literal["text", "tool_use_start", "tool_use_delta", "usage", "done"]
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    finish_reason: FinishReason | None = None
    usage: Usage | None = None


# --- embeddings ------------------------------------------------------------


class EmbeddingsRequest(BaseModel):
    """Embed one or more texts (§8.1). ``model_alias`` defaults to
    ``routing.embeddings_default`` — the provider/model swaps by config only."""

    model_config = _ALLOW_MODEL_FIELDS
    input: list[str] = Field(min_length=1)
    model_alias: str | None = None
    metadata: RequestMetadata = Field(default_factory=RequestMetadata)


class EmbeddingVector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    embedding: list[float]


class EmbeddingsResponse(BaseModel):
    model_config = _ALLOW_MODEL_FIELDS
    embeddings: list[EmbeddingVector]
    dimensions: int
    provider: str
    model_id: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int


def new_tool_call_id() -> str:
    """A stable, provider-neutral id for a tool call the provider left unnamed
    (some Ollama/OpenAI streaming frames omit ids on parallel calls)."""
    return f"tc_{uuid.uuid4().hex[:16]}"


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ContentPart",
    "EmbeddingVector",
    "EmbeddingsRequest",
    "EmbeddingsResponse",
    "FinishReason",
    "ImagePart",
    "ImageSource",
    "Message",
    "RequestMetadata",
    "Role",
    "StreamDelta",
    "TextPart",
    "ToolCall",
    "ToolChoice",
    "ToolDef",
    "ToolResultPart",
    "ToolUsePart",
    "Usage",
    "new_tool_call_id",
]
