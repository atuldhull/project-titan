"""OpenAI adapter unit tests (Phase 16) — the REQUEST wire the cassettes don't
assert: strict-mode tool schemas (TITAN-INT-061), parallel tool calls, auth.

Cassettes verify response→canonical; these capture the outgoing request via an
``httpx.MockTransport`` and assert the canonical→OpenAI translation.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from titan.llm.config import ConfigLoader
from titan.llm.errors import ProviderNotConfiguredError
from titan.llm.gateway import LlmGateway
from titan.llm.models import ChatRequest, Message, ToolDef, ToolResultPart, ToolUsePart

from .conftest import run_async

_CONFIG = """
providers:
  openai: {{ api_key: {key}, base_url: https://api.openai.com }}
model_aliases:
  strict:     {{ provider: openai, model: gpt-4.1, options: {{ strict_tools: true }} }}
  non-strict: {{ provider: openai, model: gpt-4.1 }}
routing:
  default: {default}
"""


def _gateway(
    tmp_path: Path, records: list[dict[str, Any]], *, default: str = "strict", key: str = "sk-test"
) -> LlmGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        records.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    path = tmp_path / "models.yaml"
    path.write_text(_CONFIG.format(key=key, default=default), encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LlmGateway(ConfigLoader(path), client)


_TOOL = ToolDef(
    name="get_weather",
    input_schema={
        "type": "object",
        "properties": {"city": {"type": "string"}, "units": {"type": "string"}},
        "required": ["city"],
    },
)


def _chat(gateway: LlmGateway, **kw: Any) -> None:
    run_async(
        lambda: gateway.chat(
            ChatRequest(messages=[Message(role="user", content="hi")], max_tokens=64, **kw)
        )
    )


def test_strict_mode_makes_tool_schema_strict(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    _chat(_gateway(tmp_path, records, default="strict"), tools=[_TOOL])
    fn = records[0]["tools"][0]["function"]
    assert fn["strict"] is True
    params = fn["parameters"]
    assert params["additionalProperties"] is False
    # strict mode requires EVERY property in `required`...
    assert set(params["required"]) == {"city", "units"}
    # ...but an originally-optional field stays optional by being made nullable,
    # while an originally-required one is not widened.
    assert params["properties"]["city"]["type"] == "string"
    assert params["properties"]["units"]["type"] == ["string", "null"]


def test_strict_mode_recurses_into_arrays_and_defs(tmp_path: Path) -> None:
    nested = ToolDef(
        name="plan",
        input_schema={
            "type": "object",
            "properties": {
                "stops": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"city": {"type": "string"}}},
                }
            },
            "required": ["stops"],
        },
    )
    records: list[dict[str, Any]] = []
    _chat(_gateway(tmp_path, records, default="strict"), tools=[nested])
    params = records[0]["tools"][0]["function"]["parameters"]
    item = params["properties"]["stops"]["items"]
    # The object nested under array `items` must ALSO be strict-compliant.
    assert item["additionalProperties"] is False
    assert item["required"] == ["city"]


def test_non_strict_leaves_schema_untouched(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    _chat(_gateway(tmp_path, records, default="non-strict"), tools=[_TOOL])
    fn = records[0]["tools"][0]["function"]
    assert "strict" not in fn
    assert fn["parameters"]["required"] == ["city"]  # unchanged
    assert "additionalProperties" not in fn["parameters"]


def test_missing_api_key_is_provider_not_configured(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    gateway = _gateway(tmp_path, records, default="non-strict", key='""')
    with pytest.raises(ProviderNotConfiguredError):
        _chat(gateway, tools=[_TOOL])


def test_errored_tool_result_is_marked(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    gateway = _gateway(tmp_path, records, default="non-strict")
    run_async(
        lambda: gateway.chat(
            ChatRequest(
                messages=[
                    Message(role="user", content="x"),
                    Message(role="assistant", content=[ToolUsePart(id="c1", name="t", input={})]),
                    Message(
                        role="tool",
                        content=[ToolResultPart(tool_use_id="c1", content="boom", is_error=True)],
                    ),
                ],
                max_tokens=8,
            )
        )
    )
    tool_msg = next(m for m in records[0]["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == "[tool error] boom"  # failure not shown as success


def test_bearer_auth_header_is_sent(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    path = tmp_path / "models.yaml"
    path.write_text(_CONFIG.format(key="sk-secret", default="non-strict"), encoding="utf-8")
    gateway = LlmGateway(
        ConfigLoader(path), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    _chat(gateway)
    assert captured["auth"] == "Bearer sk-secret"
