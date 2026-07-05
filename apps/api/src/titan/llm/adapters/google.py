"""Google Gemini adapter — generateContent + streaming (Vol 7 §8.1).

Gemini's wire format diverges the most, and every divergence is absorbed here so
the canonical schema never bends to it (TITAN-INT-061):

* roles are ``user``/``model`` only — the system prompt hoists to
  ``systemInstruction``; assistant→``model``; tool results ride a ``user`` turn.
* tool results correlate by function NAME, not id — the adapter rebuilds an
  id→name map from the tool_use parts already in the conversation, so the
  canonical id-based ``ToolResultPart`` still round-trips.
* function calls carry no id (we mint one) and ``finishReason`` stays ``STOP``
  even with a call present, so a call ⇒ canonical ``tool_use``.
* ``parameters`` is an OpenAPI subset — unsupported JSON-Schema keys
  (``$schema``, ``additionalProperties``) are stripped.
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
    new_tool_call_id,
)
from titan.llm.routing import ResolvedModel

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_PROVIDER = "google"

_FINISH_REASONS: dict[str, FinishReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}

# JSON-Schema keys the Gemini function-parameter schema (an OpenAPI subset) rejects.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({"$schema", "additionalProperties", "additional_properties"})


def _gemini_schema(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {
            k: _gemini_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [_gemini_schema(v) for v in schema]
    return schema


def _id_to_name(messages: list[Message]) -> dict[str, str]:
    """Map every emitted tool_use id → its function name, so a later
    ToolResultPart (id-addressed) can be re-expressed as Gemini's name-addressed
    functionResponse."""
    mapping: dict[str, str] = {}
    for message in messages:
        for part in message.parts():
            if isinstance(part, ToolUsePart):
                mapping[part.id] = part.name
    return mapping


def _contents(messages: list[Message], id_to_name: dict[str, str]) -> list[dict[str, Any]]:
    contents: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        role = "model" if message.role == "assistant" else "user"
        parts: list[dict[str, Any]] = []
        for part in message.parts():
            if isinstance(part, TextPart):
                parts.append({"text": part.text})
            elif isinstance(part, ImagePart):
                parts.append(
                    {"inlineData": {"mimeType": part.source.media_type, "data": part.source.data}}
                )
            elif isinstance(part, ToolUsePart):
                parts.append({"functionCall": {"name": part.name, "args": part.input}})
            elif isinstance(part, ToolResultPart):
                # Gemini's functionResponse.response is a struct; signal failure
                # with an 'error' key so an errored result isn't shown as success
                # (parity with Anthropic's native is_error).
                response = {"error": part.content} if part.is_error else {"result": part.content}
                parts.append(
                    {
                        "functionResponse": {
                            "name": id_to_name.get(part.tool_use_id, part.tool_use_id),
                            "response": response,
                        }
                    }
                )
        # Merge consecutive same-role turns (Gemini expects user/model alternation;
        # parallel tool results collapse into one user turn).
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})
    return contents


def _system_instruction(messages: list[Message]) -> dict[str, Any] | None:
    texts = [
        part.text
        for message in messages
        if message.role == "system"
        for part in message.parts()
        if isinstance(part, TextPart)
    ]
    return {"parts": [{"text": "\n\n".join(texts)}]} if texts else None


def _build_payload(req: ChatRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {"contents": _contents(req.messages, _id_to_name(req.messages))}
    system = _system_instruction(req.messages)
    if system is not None:
        payload["systemInstruction"] = system
    gen_config: dict[str, Any] = {"maxOutputTokens": req.max_tokens}
    if req.temperature is not None:
        gen_config["temperature"] = req.temperature
    payload["generationConfig"] = gen_config
    if req.tools:
        payload["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": _gemini_schema(t.input_schema),
                    }
                    for t in req.tools
                ]
            }
        ]
    if req.tool_choice is not None:
        mode = {"auto": "AUTO", "required": "ANY", "none": "NONE", "tool": "ANY"}[
            req.tool_choice.mode
        ]
        cfg: dict[str, Any] = {"mode": mode}
        if req.tool_choice.mode == "tool" and req.tool_choice.name:
            cfg["allowedFunctionNames"] = [req.tool_choice.name]
        payload["toolConfig"] = {"functionCallingConfig": cfg}
    return payload


def _headers(provider: ProviderConfig) -> dict[str, str]:
    if not provider.api_key:
        raise ProviderNotConfiguredError(
            "Google requires GOOGLE_API_KEY; none is configured "
            "(use the local-default alias for a zero-key profile)."
        )
    return {"x-goog-api-key": provider.api_key, "content-type": "application/json"}


def _parse_parts(parts: list[dict[str, Any]]) -> tuple[list[str], list[ToolCall]]:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for part in parts:
        if "text" in part:
            texts.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            calls.append(
                ToolCall(id=new_tool_call_id(), name=fc["name"], arguments=fc.get("args", {}) or {})
            )
    return texts, calls


def _usage(meta: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=meta.get("promptTokenCount", 0),
        output_tokens=meta.get("candidatesTokenCount", 0),
        cached_tokens=meta.get("cachedContentTokenCount", 0),
    )


def _finish(reason: str, has_calls: bool) -> FinishReason:
    # A tool call is canonically 'tool_use' even though Gemini reports STOP.
    return "tool_use" if has_calls else _FINISH_REASONS.get(reason, "stop")


def _parse_response(body: dict[str, Any]) -> ChatResult:
    candidate = (body.get("candidates") or [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    texts, calls = _parse_parts(parts)
    return ChatResult(
        text="".join(texts) or None,
        tool_calls=calls,
        finish_reason=_finish(candidate.get("finishReason", "STOP"), bool(calls)),
        usage=_usage(body.get("usageMetadata", {})),
    )


class GoogleAdapter:
    """Canonical ⇄ Gemini generateContent translation."""

    async def chat(
        self,
        client: httpx.AsyncClient,
        *,
        req: ChatRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> ChatResult:
        base = provider.base_url or _DEFAULT_BASE_URL
        url = f"{base.rstrip('/')}/v1beta/models/{resolved.model_id}:generateContent"
        try:
            resp = await client.post(
                url, headers=_headers(provider), json=_build_payload(req), timeout=_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise UpstreamProviderError(_PROVIDER, str(exc)) from exc
        if resp.status_code >= 400:
            raise UpstreamProviderError(_PROVIDER, f"{resp.status_code}: {resp.text[:500]}")
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
        url = f"{base.rstrip('/')}/v1beta/models/{resolved.model_id}:streamGenerateContent?alt=sse"
        finish: FinishReason = "stop"
        usage = Usage()
        emitted_call = False
        try:
            async with client.stream(
                "POST", url, headers=_headers(provider), json=_build_payload(req), timeout=_TIMEOUT
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode(errors="replace")[:500]
                    raise UpstreamProviderError(_PROVIDER, f"{resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = json.loads(line[5:].strip())
                    candidate = (chunk.get("candidates") or [{}])[0]
                    for part in candidate.get("content", {}).get("parts", []):
                        if "text" in part:
                            yield StreamDelta(type="text", text=part["text"])
                        elif "functionCall" in part:
                            emitted_call = True
                            fc = part["functionCall"]
                            call_id = new_tool_call_id()
                            yield StreamDelta(
                                type="tool_use_start", tool_call_id=call_id, tool_name=fc["name"]
                            )
                            yield StreamDelta(
                                type="tool_use_delta",
                                tool_call_id=call_id,
                                arguments_delta=json.dumps(fc.get("args", {}) or {}),
                            )
                    if candidate.get("finishReason"):
                        finish = _finish(candidate["finishReason"], emitted_call)
                    if chunk.get("usageMetadata"):
                        usage = _usage(chunk["usageMetadata"])
        except httpx.HTTPError as exc:
            raise UpstreamProviderError(_PROVIDER, str(exc)) from exc
        yield StreamDelta(type="done", finish_reason=finish, usage=usage)


__all__ = ["GoogleAdapter"]
