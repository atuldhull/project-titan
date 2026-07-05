"""Ollama adapter over its OpenAI-compatible endpoints (Vol 7 §8.1, §8.2).

Ollama speaks the OpenAI Chat Completions and Embeddings wire formats, so this
adapter is also the reference for how OpenAI itself will plug in (Phase 16).
Quirks handled here: tool results become discrete ``role:'tool'`` messages keyed
by ``tool_call_id``; tool-call arguments are transported as a JSON *string* and
decoded back to objects; parallel tool calls arrive as a ``tool_calls`` array.
Ollama is local and free — cost is always zero (§8.4).
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from titan.llm.adapters.base import ChatResult, EmbeddingsResult
from titan.llm.config import ProviderConfig
from titan.llm.errors import UpstreamProviderError
from titan.llm.models import (
    ChatRequest,
    EmbeddingsRequest,
    FinishReason,
    ImagePart,
    Message,
    StreamDelta,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolUsePart,
    Usage,
    new_tool_call_id,
)
from titan.llm.routing import ResolvedModel

_DEFAULT_BASE_URL = "http://localhost:11434"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_use",
    "content_filter": "content_filter",
}


def _openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            # Each tool_result becomes its own role:'tool' message (OpenAI shape).
            for part in message.parts():
                if isinstance(part, ToolResultPart):
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": part.tool_use_id,
                            "content": part.content,
                        }
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
            # Multimodal content must use the array form.
            entry["content"] = [{"type": "text", "text": "".join(text_chunks)}, *image_items]
        else:
            entry["content"] = "".join(text_chunks) if text_chunks else None
        if tool_calls:
            entry["tool_calls"] = tool_calls
        out.append(entry)
    return out


def _build_payload(req: ChatRequest, resolved: ResolvedModel, *, stream: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": resolved.model_id,
        "messages": _openai_messages(req.messages),
        "max_tokens": req.max_tokens,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    if req.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in req.tools
        ]
    if req.tool_choice is not None:
        choice = req.tool_choice
        if choice.mode == "tool" and choice.name:
            payload["tool_choice"] = {"type": "function", "function": {"name": choice.name}}
        else:  # auto / required / none map through directly
            payload["tool_choice"] = choice.mode
    return payload


def _headers(provider: ProviderConfig) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    # Ollama ignores auth, but the OpenAI-compat layer accepts a bearer token —
    # forward one when configured so the same adapter fronts a keyed endpoint.
    headers["authorization"] = f"Bearer {provider.api_key or 'ollama'}"
    return headers


def _parse_tool_calls(raw: list[dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in raw:
        fn = item.get("function", {})
        name = fn.get("name")
        if not name:
            # A tool_call with no function name is an unusable, malformed upstream
            # body — a 502 (bad dependency), not a 500 (blame the gateway).
            raise UpstreamProviderError("ollama", "tool_call missing a function name")
        raw_args = fn.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (json.JSONDecodeError, TypeError, ValueError):
            args = {}
        calls.append(ToolCall(id=item.get("id") or new_tool_call_id(), name=name, arguments=args))
    return calls


def _parse_response(body: dict[str, Any]) -> ChatResult:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message", {})
    text = message.get("content") or None
    tool_calls = _parse_tool_calls(message.get("tool_calls") or [])
    usage_raw = body.get("usage") or {}
    usage = Usage(
        input_tokens=usage_raw.get("prompt_tokens", 0),
        output_tokens=usage_raw.get("completion_tokens", 0),
        cached_tokens=(usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
    )
    finish = _FINISH_REASONS.get(choice.get("finish_reason") or "stop", "stop")
    return ChatResult(text=text, tool_calls=tool_calls, finish_reason=finish, usage=usage)


class OllamaAdapter:
    """Canonical ⇄ OpenAI-compatible (Ollama) translation, chat + embeddings."""

    async def chat(
        self,
        client: httpx.AsyncClient,
        *,
        req: ChatRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> ChatResult:
        base = provider.base_url or _DEFAULT_BASE_URL
        try:
            resp = await client.post(
                f"{base.rstrip('/')}/v1/chat/completions",
                headers=_headers(provider),
                json=_build_payload(req, resolved, stream=False),
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise UpstreamProviderError("ollama", str(exc)) from exc
        if resp.status_code >= 400:
            raise UpstreamProviderError("ollama", f"{resp.status_code}: {resp.text[:500]}")
        return _parse_response(resp.json())

    async def stream(
        self,
        client: httpx.AsyncClient,
        *,
        req: ChatRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> AsyncIterator[StreamDelta]:
        base = provider.base_url or _DEFAULT_BASE_URL
        seen_calls: dict[int, str] = {}
        finish: FinishReason = "stop"
        usage = Usage()
        try:
            async with client.stream(
                "POST",
                f"{base.rstrip('/')}/v1/chat/completions",
                headers=_headers(provider),
                json=_build_payload(req, resolved, stream=True),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode(errors="replace")[:500]
                    raise UpstreamProviderError("ollama", f"{resp.status_code}: {detail}")
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
                            cached_tokens=(u.get("prompt_tokens_details") or {}).get(
                                "cached_tokens", 0
                            ),
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
                                    type="tool_use_start",
                                    tool_call_id=call_id,
                                    tool_name=fn.get("name"),
                                )
                            if fn.get("arguments"):
                                yield StreamDelta(
                                    type="tool_use_delta",
                                    tool_call_id=seen_calls[index],
                                    arguments_delta=fn["arguments"],
                                )
                        if choice.get("finish_reason"):
                            finish = _FINISH_REASONS.get(choice["finish_reason"], "stop")
        except httpx.HTTPError as exc:
            raise UpstreamProviderError("ollama", str(exc)) from exc
        yield StreamDelta(type="done", finish_reason=finish, usage=usage)

    async def embed(
        self,
        client: httpx.AsyncClient,
        *,
        req: EmbeddingsRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> EmbeddingsResult:
        base = provider.base_url or _DEFAULT_BASE_URL
        try:
            resp = await client.post(
                f"{base.rstrip('/')}/v1/embeddings",
                headers=_headers(provider),
                json={"model": resolved.model_id, "input": req.input},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise UpstreamProviderError("ollama", str(exc)) from exc
        if resp.status_code >= 400:
            raise UpstreamProviderError("ollama", f"{resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        rows = sorted(body.get("data", []), key=lambda d: d.get("index", 0))
        vectors = [list(row.get("embedding", [])) for row in rows]
        usage_raw = body.get("usage") or {}
        usage = Usage(
            input_tokens=usage_raw.get("prompt_tokens", 0),
            output_tokens=0,
            cached_tokens=0,
        )
        return EmbeddingsResult(vectors=vectors, usage=usage)


__all__ = ["OllamaAdapter"]
