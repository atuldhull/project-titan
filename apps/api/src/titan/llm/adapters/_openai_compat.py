"""Shared OpenAI Chat Completions wire translation (Vol 7 §8.1).

Both the OpenAI adapter and the Ollama adapter (Ollama exposes an OpenAI-compatible
endpoint) speak this dialect, so the canonical⇄OpenAI translation lives here once
rather than being forked per adapter. Provider-specific bits — base URL, auth
headers, OpenAI strict-mode schemas — stay in the adapters that import this.

Quirks handled here: tool results become discrete ``role:'tool'`` messages keyed
by ``tool_call_id``; tool-call arguments ride as a JSON *string* and are decoded
back to objects; parallel tool calls arrive as a ``tool_calls`` array.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from titan.llm.adapters.base import ChatResult, EmbeddingsResult
from titan.llm.errors import UpstreamProviderError
from titan.llm.models import (
    ChatRequest,
    FinishReason,
    ImagePart,
    Message,
    StreamDelta,
    TextPart,
    ToolCall,
    ToolDef,
    ToolResultPart,
    ToolUsePart,
    Usage,
    new_tool_call_id,
)
from titan.llm.routing import ResolvedModel

_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_use",
    "content_filter": "content_filter",
}


def openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            for part in message.parts():
                if isinstance(part, ToolResultPart):
                    # OpenAI has no structured tool-error flag; mark the content so
                    # a failed tool result isn't presented to the model as success
                    # (Anthropic carries is_error natively — keep parity).
                    content = f"[tool error] {part.content}" if part.is_error else part.content
                    out.append(
                        {"role": "tool", "tool_call_id": part.tool_use_id, "content": content}
                    )
            continue

        text_chunks: list[str] = []
        image_items: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        for part in message.parts():
            if isinstance(part, TextPart):
                text_chunks.append(part.text)
            elif isinstance(part, ImagePart):
                url = f"data:{part.source.media_type};base64,{part.source.data}"
                image_items.append({"type": "image_url", "image_url": {"url": url}})
            elif isinstance(part, ToolUsePart):
                tool_calls.append(
                    {
                        "id": part.id,
                        "type": "function",
                        "function": {"name": part.name, "arguments": json.dumps(part.input)},
                    }
                )

        entry: dict[str, Any] = {"role": message.role}
        if image_items:
            entry["content"] = [{"type": "text", "text": "".join(text_chunks)}, *image_items]
        else:
            entry["content"] = "".join(text_chunks) if text_chunks else None
        if tool_calls:
            entry["tool_calls"] = tool_calls
        out.append(entry)
    return out


def _make_nullable(schema: Any) -> Any:
    """Widen a typed subschema to also accept null (OpenAI strict mode marks
    every property required, so a would-be-optional field stays optional by
    being nullable). Schemas with no plain ``type`` are left as-is."""
    if not isinstance(schema, dict):
        return schema
    t = schema.get("type")
    if isinstance(t, str) and t != "null":
        return {**schema, "type": [t, "null"]}
    if isinstance(t, list) and "null" not in t:
        return {**schema, "type": [*t, "null"]}
    return schema


def _strict_parameters(schema: Any) -> Any:
    """Make a JSON Schema OpenAI-strict-compatible everywhere it can hide an
    object (TITAN-INT-061): EVERY object subschema — including those under
    ``items``, ``$defs``/``definitions``, and ``anyOf``/``oneOf``/``allOf`` —
    must set ``additionalProperties:false`` and list all keys as required, and
    originally-optional keys are made nullable so they stay optional."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    for key in ("items", "additionalItems", "contains", "if", "then", "else", "not"):
        if key in out:
            out[key] = _strict_parameters(out[key])
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(out.get(key), list):
            out[key] = [_strict_parameters(s) for s in out[key]]
    for key in ("$defs", "definitions"):
        if isinstance(out.get(key), dict):
            out[key] = {k: _strict_parameters(v) for k, v in out[key].items()}
    if out.get("type") == "object":
        out["additionalProperties"] = False
        props = out.get("properties")
        if isinstance(props, dict):
            required = set(out.get("required", []))
            out["properties"] = {
                name: sub if name in required else _make_nullable(sub)
                for name, sub in ((n, _strict_parameters(v)) for n, v in props.items())
            }
            out["required"] = list(props.keys())
    return out


def _tool_defs(tools: list[ToolDef], *, strict: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for t in tools:
        fn: dict[str, Any] = {
            "name": t.name,
            "description": t.description,
            "parameters": _strict_parameters(t.input_schema) if strict else t.input_schema,
        }
        if strict:
            fn["strict"] = True
        result.append({"type": "function", "function": fn})
    return result


def build_chat_payload(
    req: ChatRequest, resolved: ResolvedModel, *, stream: bool, strict: bool = False
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": resolved.model_id,
        "messages": openai_messages(req.messages),
        "max_tokens": req.max_tokens,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.tools:
        payload["tools"] = _tool_defs(req.tools, strict=strict)
    if req.tool_choice is not None:
        choice = req.tool_choice
        if choice.mode == "tool" and choice.name:
            payload["tool_choice"] = {"type": "function", "function": {"name": choice.name}}
        else:  # auto / required / none map through directly
            payload["tool_choice"] = choice.mode
    return payload


def _parse_tool_calls(raw: list[dict[str, Any]], *, provider: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in raw:
        fn = item.get("function", {})
        name = fn.get("name")
        if not name:
            raise UpstreamProviderError(provider, "tool_call missing a function name")
        raw_args = fn.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (json.JSONDecodeError, TypeError, ValueError):
            args = {}
        calls.append(ToolCall(id=item.get("id") or new_tool_call_id(), name=name, arguments=args))
    return calls


def parse_chat_response(body: dict[str, Any], *, provider: str) -> ChatResult:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message", {})
    text = message.get("content") or None
    tool_calls = _parse_tool_calls(message.get("tool_calls") or [], provider=provider)
    usage_raw = body.get("usage") or {}
    usage = Usage(
        input_tokens=usage_raw.get("prompt_tokens", 0),
        output_tokens=usage_raw.get("completion_tokens", 0),
        cached_tokens=(usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
    )
    finish = _FINISH_REASONS.get(choice.get("finish_reason") or "stop", "stop")
    return ChatResult(text=text, tool_calls=tool_calls, finish_reason=finish, usage=usage)


async def parse_chat_stream(resp: httpx.Response) -> AsyncIterator[StreamDelta]:
    """Normalize an OpenAI SSE stream into canonical :class:`StreamDelta` events.
    The caller opens the stream and checks status; this only parses ``data:`` lines."""
    seen_calls: dict[int, str] = {}
    finish: FinishReason = "stop"
    usage = Usage()
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        chunk = json.loads(payload)
        if chunk.get("usage"):
            u = chunk["usage"]
            usage = Usage(
                input_tokens=u.get("prompt_tokens", 0),
                output_tokens=u.get("completion_tokens", 0),
                cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            )
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content"):
                yield StreamDelta(type="text", text=delta["content"])
            for call in delta.get("tool_calls", []):
                index = call.get("index", 0)
                fn = call.get("function", {})
                if index not in seen_calls:
                    call_id = call.get("id") or new_tool_call_id()
                    seen_calls[index] = call_id
                    yield StreamDelta(
                        type="tool_use_start", tool_call_id=call_id, tool_name=fn.get("name")
                    )
                if fn.get("arguments"):
                    yield StreamDelta(
                        type="tool_use_delta",
                        tool_call_id=seen_calls[index],
                        arguments_delta=fn["arguments"],
                    )
            if choice.get("finish_reason"):
                finish = _FINISH_REASONS.get(choice["finish_reason"], "stop")
    yield StreamDelta(type="done", finish_reason=finish, usage=usage)


def parse_embeddings(body: dict[str, Any]) -> EmbeddingsResult:
    rows = sorted(body.get("data", []), key=lambda d: d.get("index", 0))
    vectors = [list(row.get("embedding", [])) for row in rows]
    usage_raw = body.get("usage") or {}
    usage = Usage(input_tokens=usage_raw.get("prompt_tokens", 0), output_tokens=0, cached_tokens=0)
    return EmbeddingsResult(vectors=vectors, usage=usage)


__all__ = [
    "build_chat_payload",
    "openai_messages",
    "parse_chat_response",
    "parse_chat_stream",
    "parse_embeddings",
]
