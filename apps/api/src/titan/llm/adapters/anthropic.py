"""Anthropic Messages API adapter (Vol 7 §8.1).

Quirks handled here, behind the canonical contract: the system prompt is hoisted
to a top-level ``system`` field; tool results are carried on ``user`` turns (not
a distinct ``tool`` role); parallel tool calls arrive as multiple ``tool_use``
content blocks. Prompt-cache reads surface as ``usage.cached_tokens``.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from titan.llm.adapters.base import ChatResult
from titan.llm.config import ProviderConfig
from titan.llm.errors import ProviderNotConfiguredError, UpstreamProviderError
from titan.llm.models import (
    ChatRequest,
    FinishReason,
    ImagePart,
    Message,
    StreamDelta,
    TextPart,
    ToolCall,
    ToolResultPart,
    ToolUsePart,
    Usage,
)
from titan.llm.routing import ResolvedModel

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)

_STOP_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_use",
}


def _anthropic_role(message: Message) -> str:
    # Anthropic has no `tool` role: tool results ride on a `user` turn.
    return "user" if message.role in ("user", "tool") else "assistant"


def _content_blocks(message: Message) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in message.parts():
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.source.media_type,
                        "data": part.source.data,
                    },
                }
            )
        elif isinstance(part, ToolUsePart):
            blocks.append(
                {"type": "tool_use", "id": part.id, "name": part.name, "input": part.input}
            )
        elif isinstance(part, ToolResultPart):
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": part.tool_use_id,
                    "content": part.content,
                    "is_error": part.is_error,
                }
            )
    return blocks


def _build_payload(req: ChatRequest, resolved: ResolvedModel) -> dict[str, Any]:
    system_text: list[str] = []
    turns: list[dict[str, Any]] = []
    for message in req.messages:
        if message.role == "system":
            system_text.extend(part.text for part in message.parts() if isinstance(part, TextPart))
            continue
        role = _anthropic_role(message)
        blocks = _content_blocks(message)
        # Merge consecutive same-role turns (e.g. parallel tool_results) so the
        # user/assistant alternation Anthropic requires is preserved.
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})

    payload: dict[str, Any] = {
        "model": resolved.model_id,
        "messages": turns,
        "max_tokens": req.max_tokens,
    }
    if system_text:
        payload["system"] = "\n\n".join(system_text)
    if req.temperature is not None:
        # The canonical range is [0, 2] (OpenAI-family); Anthropic accepts only
        # [0, 1] and 400s above it. Clamp here so a schema-valid request never
        # fails on an Anthropic route — the quirk stays inside the adapter.
        payload["temperature"] = min(req.temperature, 1.0)
    if req.tools:
        payload["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in req.tools
        ]
    if req.tool_choice is not None:
        choice = req.tool_choice
        if choice.mode == "tool" and choice.name:
            payload["tool_choice"] = {"type": "tool", "name": choice.name}
        elif choice.mode == "required":
            payload["tool_choice"] = {"type": "any"}
        else:  # auto / none
            payload["tool_choice"] = {"type": choice.mode}
    return payload


def _headers(provider: ProviderConfig) -> dict[str, str]:
    if not provider.api_key:
        raise ProviderNotConfiguredError(
            "Anthropic requires ANTHROPIC_API_KEY; none is configured "
            "(use the local-default alias for a zero-key profile)."
        )
    return {
        "x-api-key": provider.api_key,
        "anthropic-version": _API_VERSION,
        "content-type": "application/json",
    }


def _parse_response(body: dict[str, Any]) -> ChatResult:
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in body.get("content", []):
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block["id"],
                    name=block["name"],
                    arguments=block.get("input", {}) or {},
                )
            )
    usage_raw = body.get("usage", {})
    usage = Usage(
        input_tokens=usage_raw.get("input_tokens", 0),
        output_tokens=usage_raw.get("output_tokens", 0),
        cached_tokens=usage_raw.get("cache_read_input_tokens", 0),
    )
    finish = _STOP_REASONS.get(body.get("stop_reason", ""), "stop")
    return ChatResult(
        text="".join(texts) or None,
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=usage,
    )


class AnthropicAdapter:
    """Canonical ⇄ Anthropic Messages API translation."""

    async def chat(
        self,
        client: httpx.AsyncClient,
        *,
        req: ChatRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> ChatResult:
        base = provider.base_url or _DEFAULT_BASE_URL
        payload = _build_payload(req, resolved)
        try:
            resp = await client.post(
                f"{base.rstrip('/')}/v1/messages",
                headers=_headers(provider),
                json=payload,
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise UpstreamProviderError("anthropic", str(exc)) from exc
        if resp.status_code >= 400:
            raise UpstreamProviderError("anthropic", f"{resp.status_code}: {resp.text[:500]}")
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
        payload = _build_payload(req, resolved) | {"stream": True}
        headers = _headers(provider)
        block_ids: dict[int, str] = {}
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        finish: FinishReason = "stop"
        try:
            async with client.stream(
                "POST",
                f"{base.rstrip('/')}/v1/messages",
                headers=headers,
                json=payload,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode(errors="replace")[:500]
                    raise UpstreamProviderError("anthropic", f"{resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:].strip())
                    etype = event.get("type")
                    if etype == "message_start":
                        input_tokens = event["message"]["usage"].get("input_tokens", 0)
                        cached_tokens = event["message"]["usage"].get("cache_read_input_tokens", 0)
                    elif etype == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "tool_use":
                            block_ids[event["index"]] = block["id"]
                            yield StreamDelta(
                                type="tool_use_start",
                                tool_call_id=block["id"],
                                tool_name=block["name"],
                            )
                    elif etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield StreamDelta(type="text", text=delta.get("text", ""))
                        elif delta.get("type") == "input_json_delta":
                            yield StreamDelta(
                                type="tool_use_delta",
                                tool_call_id=block_ids.get(event["index"]),
                                arguments_delta=delta.get("partial_json", ""),
                            )
                    elif etype == "message_delta":
                        finish = _STOP_REASONS.get(
                            event.get("delta", {}).get("stop_reason", ""), "stop"
                        )
                        output_tokens = event.get("usage", {}).get("output_tokens", output_tokens)
        except httpx.HTTPError as exc:
            raise UpstreamProviderError("anthropic", str(exc)) from exc
        usage = Usage(
            input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens
        )
        yield StreamDelta(type="done", finish_reason=finish, usage=usage)


__all__ = ["AnthropicAdapter"]
