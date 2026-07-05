"""Google/Gemini adapter unit tests (Phase 16) — the request-side quirks the
cassettes don't assert: system hoisting, role mapping, tool-result correlation
by NAME (not id), OpenAPI-subset schema sanitizing, STOP-with-call → tool_use.
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

from . import _transcript as T
from .conftest import run_async

_CONFIG = """
providers:
  google: {{ api_key: {key}, base_url: https://generativelanguage.googleapis.com }}
model_aliases:
  g: {{ provider: google, model: gemini-2.5-flash }}
routing:
  default: g
"""


def _two_functioncalls() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}},
                        {"functionCall": {"name": "get_time", "args": {"city": "Paris"}}},
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 15},
    }


def _final_text() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "It is sunny in Paris."}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 8},
    }


def _gateway(tmp_path: Path, records: list[dict[str, Any]], *, key: str = "g-key") -> LlmGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        records.append(body)
        has_result = any(
            "functionResponse" in part
            for content in body.get("contents", [])
            for part in content.get("parts", [])
        )
        return httpx.Response(200, json=_final_text() if has_result else _two_functioncalls())

    path = tmp_path / "models.yaml"
    path.write_text(_CONFIG.format(key=key), encoding="utf-8")
    return LlmGateway(ConfigLoader(path), httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_gemini_request_shape_and_name_correlation(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    gateway = _gateway(tmp_path, records)
    r1, r2 = T.run_golden_transcript(gateway)

    # STOP with function calls is canonically tool_use, ids minted by the adapter.
    T.assert_golden_canonical(r1, r2, provider="google", model_id="gemini-2.5-flash")

    turn1 = records[0]
    assert turn1["systemInstruction"]["parts"][0]["text"] == "You are helpful."
    assert turn1["generationConfig"]["maxOutputTokens"] == 1024
    assert turn1["contents"][0]["role"] == "user"  # no 'system' role in contents

    turn2 = records[1]
    # The assistant tool_use echo becomes a 'model' turn with functionCall parts.
    model_turn = next(c for c in turn2["contents"] if c["role"] == "model")
    assert [p["functionCall"]["name"] for p in model_turn["parts"]] == ["get_weather", "get_time"]
    # Tool results correlate by NAME (resolved from the minted ids), not the id.
    responses = [
        p["functionResponse"]
        for c in turn2["contents"]
        for p in c["parts"]
        if "functionResponse" in p
    ]
    assert {fr["name"] for fr in responses} == {"get_weather", "get_time"}
    assert all(fr["response"] == {"result": "ok"} for fr in responses)
    # None of the functionResponse names is a raw canonical tool-call id.
    assert not any(fr["name"].startswith("tc_") for fr in responses)


def test_gemini_schema_sanitizing(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    gateway = _gateway(tmp_path, records)
    tool = ToolDef(
        name="lookup",
        input_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {"q": {"type": "string"}},
        },
    )
    run_async(
        lambda: gateway.chat(
            ChatRequest(messages=[Message(role="user", content="hi")], max_tokens=8, tools=[tool])
        )
    )
    params = records[0]["tools"][0]["functionDeclarations"][0]["parameters"]
    assert "$schema" not in params
    assert "additionalProperties" not in params
    assert params["properties"]["q"] == {"type": "string"}


def test_gemini_errored_tool_result_uses_error_key(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    gateway = _gateway(tmp_path, records)
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
    fr = next(
        p["functionResponse"]
        for c in records[0]["contents"]
        for p in c["parts"]
        if "functionResponse" in p
    )
    assert fr["response"] == {"error": "boom"}  # failure signalled, not {result: ...}


def test_gemini_missing_key_is_provider_not_configured(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    gateway = _gateway(tmp_path, records, key='""')
    with pytest.raises(ProviderNotConfiguredError):
        run_async(
            lambda: gateway.chat(
                ChatRequest(messages=[Message(role="user", content="hi")], max_tokens=8)
            )
        )
