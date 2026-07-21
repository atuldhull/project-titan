"""Request-shape redaction for the gateway (TITAN-SEC-054 sink a).

Walks the canonical DTOs and redacts every payload-bearing string BEFORE any
adapter turns the request into provider wire format: message text (system
prompts included — the thread transcript, memory packet, and intake brief all
travel as messages), tool-result content, tool-call arguments, and embedding
inputs. ``ToolDef`` schemas/descriptions are code-authored constants and skip
the walk; image parts carry base64 pixels, not text.

Redacting HERE (not in the adapters, not on the httpx transport) covers all
three entrypoints — ``chat``, ``stream``, ``embed`` — for every provider in
the fallback chain, plus the ``/api/v1/llm/*`` HTTP surface, with one seam.
"""

from titan.llm.models import (
    ChatRequest,
    ContentPart,
    EmbeddingsRequest,
    ImagePart,
    Message,
    TextPart,
    ToolResultPart,
    ToolUsePart,
)
from titan.redaction import RedactionEngine, redact_structure

_SINK = "gateway"


def _redact_part(engine: RedactionEngine, part: ContentPart) -> ContentPart:
    if isinstance(part, TextPart):
        return TextPart(text=engine.redact(part.text, sink=_SINK))
    if isinstance(part, ToolResultPart):
        return ToolResultPart(
            tool_use_id=part.tool_use_id,
            content=engine.redact(part.content, sink=_SINK),
            is_error=part.is_error,
        )
    if isinstance(part, ToolUsePart):
        input_redacted = redact_structure(part.input, engine=engine, sink=_SINK)
        return ToolUsePart(id=part.id, name=part.name, input=input_redacted)
    assert isinstance(part, ImagePart)
    return part


def _redact_message(engine: RedactionEngine, message: Message) -> Message:
    if isinstance(message.content, str):
        return Message(role=message.role, content=engine.redact(message.content, sink=_SINK))
    return Message(
        role=message.role,
        content=[_redact_part(engine, part) for part in message.content],
    )


def redact_chat_request(engine: RedactionEngine, req: ChatRequest) -> ChatRequest:
    return req.model_copy(update={"messages": [_redact_message(engine, m) for m in req.messages]})


def redact_embeddings_request(engine: RedactionEngine, req: EmbeddingsRequest) -> EmbeddingsRequest:
    return req.model_copy(update={"input": [engine.redact(text, sink=_SINK) for text in req.input]})


__all__ = ["redact_chat_request", "redact_embeddings_request"]
