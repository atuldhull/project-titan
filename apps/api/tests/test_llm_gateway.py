"""LLM gateway tests (Phase 15; Vol 7 §8, TITAN-INT-060/061/062).

These run WITHOUT a database or network: every provider call is served by an
``httpx.MockTransport`` wire fixture. They cover the phase's definition of done —

  * a golden multi-tool, multi-turn transcript round-trips through BOTH the
    Anthropic (Messages API) and Ollama (OpenAI-compatible) adapters, including
    parallel tool calls (TITAN-INT-061);
  * an embeddings round-trip returns 768-d vectors on the zero-key local profile;
  * switching a provider for a request is a models.yaml edit only, no code
    change (TITAN-INT-060), and is picked up hot (no restart);
  * the provider-SDK chokepoint holds (TITAN-INT-002 / ADR-007).

The same golden transcript and embeddings round-trip run against LIVE Ollama in
``test_llm_ollama_live.py`` (skipped when Ollama is absent).
"""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from titan.llm import service as llm
from titan.llm.config import ConfigLoader, parse_config
from titan.llm.errors import (
    ProviderNotConfiguredError,
    UnknownAliasError,
    UpstreamProviderError,
)
from titan.llm.gateway import LlmGateway, compute_cost
from titan.llm.models import (
    ChatRequest,
    Message,
    StreamDelta,
    TextPart,
    Usage,
)

from . import _transcript as T
from .conftest import run_async

# --- config fixtures -------------------------------------------------------

_FULL_CONFIG = """
providers:
  anthropic: { api_key: test-anthropic-key, base_url: https://api.anthropic.com }
  google:    { api_key: "${GOOGLE_API_KEY}" }
  ollama:    { base_url: http://ollama-test:11434 }
model_aliases:
  coding-default:
    provider: anthropic
    model: claude-sonnet-4-5
    price: { input_per_mtok: 3.00, output_per_mtok: 15.00 }
  fast-cheap:     { provider: google,    model: gemini-2.5-flash }
  local-default:  { provider: ollama,    model: qwen3-coder:30b }
  embed-default:  { provider: ollama,    model: nomic-embed-text, dimensions: 768 }
routing:
  default: coding-default
  embeddings_default: embed-default
  by_agent:
    agent.qa: local-default
  fallbacks:
    coding-default: [local-default]
"""


def _write_config(tmp_path: Path, text: str = _FULL_CONFIG, name: str = "models.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _gateway(path: Path, handler: Callable[[httpx.Request], httpx.Response]) -> LlmGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LlmGateway(ConfigLoader(path), client)


# --- wire-fixture handlers -------------------------------------------------


def _make_handler(
    records: list[dict[str, Any]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """One transport fronting both providers, dispatched by URL path. Records
    each decoded request body when ``records`` is supplied (round-trip asserts)."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if records is not None:
            records.append(body)
        path = request.url.path
        if path.endswith("/v1/messages"):
            return _anthropic(body)
        if path.endswith("/v1/chat/completions"):
            return _ollama_chat(body)
        if path.endswith("/v1/embeddings"):
            return _ollama_embeddings(body)
        return httpx.Response(404, json={"error": "unhandled path"})

    return handler


def _anthropic(body: dict[str, Any]) -> httpx.Response:
    has_tool_result = any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_result" for b in m["content"])
        for m in body["messages"]
    )
    if not has_tool_result:
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check both."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    },
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "get_time",
                        "input": {"city": "Paris"},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 42, "output_tokens": 18, "cache_read_input_tokens": 5},
            },
        )
    return httpx.Response(
        200,
        json={
            "id": "msg_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "It is 20C and 14:00 in Paris."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 80, "output_tokens": 12, "cache_read_input_tokens": 0},
        },
    )


def _ollama_chat(body: dict[str, Any]) -> httpx.Response:
    has_tool_result = any(m.get("role") == "tool" for m in body["messages"])
    if not has_tool_result:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Checking both.",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city": "Paris"}',
                                    },
                                },
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "get_time",
                                        "arguments": '{"city": "Paris"}',
                                    },
                                },
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 20},
            },
        )
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": "It is 20C and 14:00 in Paris."},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 75, "completion_tokens": 10},
        },
    )


def _ollama_embeddings(body: dict[str, Any]) -> httpx.Response:
    inputs = body["input"]
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": body["model"],
            "data": [
                {"object": "embedding", "index": i, "embedding": [round(0.001 * i, 4)] * 768}
                for i in range(len(inputs))
            ],
            "usage": {"prompt_tokens": 8, "total_tokens": 8},
        },
    )


# --- config parsing / validation / hot reload ------------------------------


def test_full_config_parses_and_resolves_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "g-secret")
    cfg = parse_config(_FULL_CONFIG)
    assert cfg.providers["google"].api_key == "g-secret"
    assert cfg.model_aliases["coding-default"].provider == "anthropic"
    assert cfg.routing.fallbacks["coding-default"] == ["local-default"]


def test_missing_env_ref_resolves_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    cfg = parse_config(_FULL_CONFIG)
    # A missing paid key must NOT break loading — the zero-key local profile
    # depends on this.
    assert cfg.providers["google"].api_key == ""


@pytest.mark.parametrize(
    "mutation",
    [
        "routing:\n  default: nope\n",
        "routing:\n  default: coding-default\n  by_agent:\n    agent.x: nope\n",
        "routing:\n  default: coding-default\n  fallbacks:\n    coding-default: [nope]\n",
    ],
)
def test_invalid_references_are_rejected(mutation: str) -> None:
    base = (
        "providers:\n  ollama: { base_url: x }\n  anthropic: { api_key: k }\n"
        "model_aliases:\n  coding-default: { provider: anthropic, model: m }\n"
    )
    with pytest.raises(ValueError):
        parse_config(base + mutation)


def test_alias_with_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        parse_config(
            "providers:\n  ollama: { base_url: x }\n"
            "model_aliases:\n  a: { provider: ghost, model: m }\n"
            "routing:\n  default: a\n"
        )


def test_local_profile_needs_no_keys() -> None:
    cfg = parse_config(
        "providers:\n  ollama: { base_url: http://ollama:11434 }\n"
        "model_aliases:\n"
        "  local-default: { provider: ollama, model: qwen3-coder:30b }\n"
        "  embed-default: { provider: ollama, model: nomic-embed-text, dimensions: 768 }\n"
        "routing:\n  default: local-default\n  embeddings_default: embed-default\n"
    )
    assert cfg.routing.default == "local-default"


def test_config_hot_reloads_on_mtime_change(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    loader = ConfigLoader(path)
    assert loader.get().routing.default == "coding-default"

    swapped = _FULL_CONFIG.replace("default: coding-default", "default: local-default")
    path.write_text(swapped, encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))  # force a distinct mtime
    assert loader.get().routing.default == "local-default"


# --- routing precedence ----------------------------------------------------


def test_routing_precedence(tmp_path: Path) -> None:
    from titan.llm.routing import ModelRouter

    router = ModelRouter(ConfigLoader(_write_config(tmp_path)).get())
    # explicit alias wins over everything
    assert (
        router.resolve_chat(model_alias="local-default", agent_id="agent.qa").provider == "ollama"
    )
    # agent routing beats the default
    assert router.resolve_chat(model_alias=None, agent_id="agent.qa").alias == "local-default"
    # else the default
    assert router.resolve_chat(model_alias=None, agent_id=None).alias == "coding-default"
    # unknown agent falls through to default
    assert router.resolve_chat(model_alias=None, agent_id="agent.unknown").alias == "coding-default"
    # embeddings default
    assert router.resolve_embeddings(model_alias=None).alias == "embed-default"
    assert router.fallback_chain("coding-default") == ["local-default"]


# --- golden multi-tool, multi-turn transcript (TITAN-INT-061) --------------


def _golden_transcript(
    gateway: LlmGateway, records: list[dict[str, Any]], *, provider: str, model_id: str
) -> None:
    """Drive the shared golden transcript (``tests/_transcript.py``) and assert the
    canonical result, plus that the second upstream request carried the results."""
    r1, r2 = T.run_golden_transcript(gateway)
    T.assert_golden_canonical(r1, r2, provider=provider, model_id=model_id)
    assert len(records) == 2  # two upstream calls; wire shape asserted by callers


def test_golden_transcript_anthropic(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    gateway = _gateway(_write_config(tmp_path), _make_handler(records))
    _golden_transcript(gateway, records, provider="anthropic", model_id="claude-sonnet-4-5")
    second = records[1]
    # Anthropic quirk: the two parallel tool_results are merged into ONE user
    # turn (alternation preserved), following the assistant tool_use turn.
    assert second["messages"][-2]["role"] == "assistant"
    tool_uses = [b for b in second["messages"][-2]["content"] if b["type"] == "tool_use"]
    assert len(tool_uses) == 2
    assert second["messages"][-1]["role"] == "user"
    results = [b for b in second["messages"][-1]["content"] if b["type"] == "tool_result"]
    assert {b["tool_use_id"] for b in results} == {"toolu_1", "toolu_2"}


def test_golden_transcript_ollama_wire(tmp_path: Path) -> None:
    cfg = _FULL_CONFIG.replace("default: coding-default", "default: local-default")
    records: list[dict[str, Any]] = []
    gateway = _gateway(_write_config(tmp_path, cfg), _make_handler(records))
    _golden_transcript(gateway, records, provider="ollama", model_id="qwen3-coder:30b")
    second = records[1]
    # OpenAI/Ollama quirk: tool results are discrete role:'tool' messages keyed
    # by tool_call_id (NOT merged into a user turn).
    tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"call_1", "call_2"}
    assistant = next(
        m for m in second["messages"] if m["role"] == "assistant" and m.get("tool_calls")
    )
    assert len(assistant["tool_calls"]) == 2


# --- provider switch is config-only (TITAN-INT-060) ------------------------


def test_switching_provider_is_config_only(tmp_path: Path) -> None:
    """The SAME request and SAME code hit a different provider after editing
    ONLY routing.default in models.yaml — proving config-only switching."""
    path = _write_config(tmp_path)
    gateway = _gateway(path, _make_handler())

    req = ChatRequest(messages=[Message(role="user", content="hi")], max_tokens=64)
    before = run_async(lambda: gateway.chat(req))
    assert before.provider == "anthropic"
    assert before.model_id == "claude-sonnet-4-5"

    # Edit models.yaml only (no code change) and force a hot reload.
    path.write_text(
        _FULL_CONFIG.replace("default: coding-default", "default: local-default"),
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))

    after = run_async(lambda: gateway.chat(req))
    assert after.provider == "ollama"
    assert after.model_id == "qwen3-coder:30b"


# --- embeddings round-trip: 768-d, zero-key local profile ------------------


def test_embeddings_returns_768d_zero_key(tmp_path: Path) -> None:
    # Local-only profile: Ollama alone, no paid keys anywhere.
    cfg = (
        "providers:\n  ollama: { base_url: http://ollama-test:11434 }\n"
        "model_aliases:\n"
        "  embed-default: { provider: ollama, model: nomic-embed-text, dimensions: 768 }\n"
        "routing:\n  default: embed-default\n  embeddings_default: embed-default\n"
    )
    gateway = _gateway(_write_config(tmp_path, cfg), _make_handler())
    from titan.llm.models import EmbeddingsRequest

    resp = run_async(lambda: gateway.embed(EmbeddingsRequest(input=["hello", "world", "titan"])))
    assert resp.dimensions == 768
    assert len(resp.embeddings) == 3
    assert all(len(v.embedding) == 768 for v in resp.embeddings)
    assert [v.index for v in resp.embeddings] == [0, 1, 2]
    assert resp.provider == "ollama"
    assert resp.model_id == "nomic-embed-text"
    assert resp.cost_usd == 0  # Ollama is free


# --- streaming normalized to one delta shape (§8.1) ------------------------


def _stream_response(lines: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    body = "".join(f"data: {line}\n\n" for line in lines).encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return handler


def test_streaming_anthropic_normalized(tmp_path: Path) -> None:
    lines = [
        '{"type":"message_start","message":{"usage":{"input_tokens":30,"cache_read_input_tokens":4}}}',
        '{"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
        '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi "}}',
        '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"there"}}',
        '{"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_9","name":"get_weather"}}',
        '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"city\\""}}',
        '{"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":":\\"Paris\\"}"}}',
        '{"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":15}}',
        '{"type":"message_stop"}',
    ]
    gateway = _gateway(_write_config(tmp_path), _stream_response(lines))
    deltas = _collect_stream(
        gateway,
        ChatRequest(messages=[Message(role="user", content="hi")], max_tokens=64, stream=True),
    )
    _assert_stream(deltas, cached=4)


def test_streaming_ollama_normalized(tmp_path: Path) -> None:
    cfg = _FULL_CONFIG.replace("default: coding-default", "default: local-default")
    lines = [
        '{"choices":[{"delta":{"content":"Hi "}}]}',
        '{"choices":[{"delta":{"content":"there"}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_9","function":{"name":"get_weather","arguments":"{\\"city\\""}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"Paris\\"}"}}]}}]}',
        '{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        '{"usage":{"prompt_tokens":30,"completion_tokens":15}}',
        "[DONE]",
    ]
    gateway = _gateway(_write_config(tmp_path, cfg), _stream_response(lines))
    deltas = _collect_stream(
        gateway,
        ChatRequest(messages=[Message(role="user", content="hi")], max_tokens=64, stream=True),
    )
    _assert_stream(deltas, cached=0)


def _collect_stream(gateway: LlmGateway, req: ChatRequest) -> list[StreamDelta]:
    async def run() -> list[StreamDelta]:
        return [d async for d in gateway.stream(req)]

    result: list[StreamDelta] = run_async(run)
    return result


def _assert_stream(deltas: list[StreamDelta], *, cached: int) -> None:
    text = "".join(d.text or "" for d in deltas if d.type == "text")
    assert text == "Hi there"
    starts = [d for d in deltas if d.type == "tool_use_start"]
    assert len(starts) == 1 and starts[0].tool_name == "get_weather"
    args = "".join(d.arguments_delta or "" for d in deltas if d.type == "tool_use_delta")
    assert args == '{"city":"Paris"}'
    done = deltas[-1]
    assert done.type == "done"
    assert done.finish_reason == "tool_use"
    assert done.usage is not None
    assert done.usage.output_tokens == 15
    assert done.usage.cached_tokens == cached


# --- error paths -----------------------------------------------------------


def test_unknown_alias_is_422(tmp_path: Path) -> None:
    gateway = _gateway(_write_config(tmp_path), _make_handler())
    req = ChatRequest(
        messages=[Message(role="user", content="x")], max_tokens=8, model_alias="ghost"
    )
    with pytest.raises(UnknownAliasError) as exc:
        run_async(lambda: gateway.chat(req))
    assert exc.value.status == 422


def test_unsupported_provider_is_500(tmp_path: Path) -> None:
    # fast-cheap → google, which has no adapter until Phase 16.
    gateway = _gateway(_write_config(tmp_path), _make_handler())
    req = ChatRequest(
        messages=[Message(role="user", content="x")], max_tokens=8, model_alias="fast-cheap"
    )
    with pytest.raises(ProviderNotConfiguredError):
        run_async(lambda: gateway.chat(req))


def test_missing_key_falls_back_to_local(tmp_path: Path) -> None:
    # A missing Anthropic key is a provider failure — with a fallback chain the
    # gateway recovers on local Ollama, tagged degraded (TITAN-OPS-022).
    cfg = _FULL_CONFIG.replace("api_key: test-anthropic-key, ", "")
    gateway = _gateway(_write_config(tmp_path, cfg), _make_handler())
    req = ChatRequest(messages=[Message(role="user", content="x")], max_tokens=8)
    resp = run_async(lambda: gateway.chat(req))
    assert resp.provider == "ollama"
    assert resp.requested_alias == "coding-default"
    assert resp.served_alias == "local-default"
    assert resp.degraded_model is True


def test_missing_key_without_fallback_is_500(tmp_path: Path) -> None:
    # No fallback chain → the misconfiguration surfaces as ProviderNotConfigured.
    cfg = (
        "providers:\n  anthropic: { base_url: https://api.anthropic.com }\n"
        "model_aliases:\n  only: { provider: anthropic, model: claude-sonnet-4-5 }\n"
        "routing:\n  default: only\n"
    )
    gateway = _gateway(_write_config(tmp_path, cfg), _make_handler())
    req = ChatRequest(messages=[Message(role="user", content="x")], max_tokens=8)
    with pytest.raises(ProviderNotConfiguredError):
        run_async(lambda: gateway.chat(req))


def test_all_upstream_down_is_503(tmp_path: Path) -> None:
    # Both the primary and its fallback 5xx → one paused_provider signal (503),
    # not the raw per-provider 502 (TITAN-FND-053).
    from titan.llm.errors import AllProvidersDownError

    def boom(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    gateway = _gateway(_write_config(tmp_path), boom)
    req = ChatRequest(messages=[Message(role="user", content="x")], max_tokens=8)
    with pytest.raises(AllProvidersDownError) as exc:
        run_async(lambda: gateway.chat(req))
    assert exc.value.status == 503


# --- cost accounting -------------------------------------------------------


def test_cost_computation() -> None:
    from decimal import Decimal

    from titan.llm.config import Price

    price = Price(input_per_mtok=Decimal("3.00"), output_per_mtok=Decimal("15.00"))
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert compute_cost(price, usage) == Decimal("18.00")
    assert compute_cost(None, usage) == Decimal(0)  # unpriced ⇒ free


def test_bare_string_content_normalizes_to_text_part() -> None:
    msg = Message(role="user", content="just text")
    parts = msg.parts()
    assert len(parts) == 1 and isinstance(parts[0], TextPart)
    assert parts[0].text == "just text"


# --- provider-SDK chokepoint (TITAN-INT-002 / ADR-007) ---------------------


def test_provider_sdk_chokepoint() -> None:
    """No module outside titan/llm may import a provider SDK. This complements
    the import-linter contract with a fast, self-contained proof."""
    banned = ("anthropic", "openai", "google.genai", "google.generativeai", "ollama")
    src = Path(__file__).resolve().parents[1] / "src" / "titan"
    offenders: list[str] = []
    for py in src.rglob("*.py"):
        if "llm" in py.relative_to(src).parts:
            continue  # the gateway IS the permitted home for provider SDKs
        text = py.read_text(encoding="utf-8")
        for name in banned:
            root = name.split(".")[0]
            if f"import {root}" in text and f"titan.{root}" not in text:
                offenders.append(f"{py}: imports {root}")
    assert not offenders, offenders


def test_build_gateway_from_settings(tmp_path: Path) -> None:
    from titan.settings import Settings

    path = _write_config(tmp_path)
    settings = Settings(models_config_path=str(path), _env_file=None)
    transport = httpx.MockTransport(_make_handler())
    gateway = llm.build_gateway(settings, client=httpx.AsyncClient(transport=transport))
    resp = run_async(
        lambda: gateway.chat(
            ChatRequest(messages=[Message(role="user", content="hi")], max_tokens=8)
        )
    )
    assert resp.provider == "anthropic"
    run_async(gateway.aclose)


# --- adversarial-review regressions ----------------------------------------


def test_anthropic_clamps_temperature_to_provider_range(tmp_path: Path) -> None:
    """Canonical temperature is [0, 2]; Anthropic accepts only [0, 1]. A
    schema-valid 1.5 must be clamped in the adapter, not 400'd upstream."""
    records: list[dict[str, Any]] = []
    gateway = _gateway(_write_config(tmp_path), _make_handler(records))
    req = ChatRequest(messages=[Message(role="user", content="hi")], max_tokens=8, temperature=1.5)
    run_async(lambda: gateway.chat(req))
    assert records[0]["temperature"] == 1.0


def test_ollama_nameless_tool_call_is_upstream_error(tmp_path: Path) -> None:
    """A malformed provider body (tool_call with no name) is a 502, not a 500."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {"id": "x", "type": "function", "function": {"arguments": "{}"}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    cfg = _FULL_CONFIG.replace("default: coding-default", "default: local-default")
    gateway = _gateway(_write_config(tmp_path, cfg), handler)
    req = ChatRequest(messages=[Message(role="user", content="x")], max_tokens=8)
    with pytest.raises(UpstreamProviderError) as exc:
        run_async(lambda: gateway.chat(req))
    assert exc.value.status == 502


def test_hot_reload_keeps_last_good_config_on_bad_edit(tmp_path: Path) -> None:
    """A malformed hot edit must not 500 all traffic — serve the last-good config."""
    path = _write_config(tmp_path)
    loader = ConfigLoader(path)
    assert loader.get().routing.default == "coding-default"

    path.write_text("this: is: not: valid: yaml: [", encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))
    # No raise — the previously-parsed good config is still served.
    assert loader.get().routing.default == "coding-default"

    # And a subsequent VALID edit is picked up (mtime advances again).
    path.write_text(
        _FULL_CONFIG.replace("default: coding-default", "default: local-default"),
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, (stat.st_atime + 20, stat.st_mtime + 20))
    assert loader.get().routing.default == "local-default"


def test_tool_choice_named_mode_requires_name() -> None:
    from pydantic import ValidationError

    from titan.llm.models import ToolChoice

    ToolChoice(mode="tool", name="get_weather")  # ok
    ToolChoice(mode="auto")  # ok
    with pytest.raises(ValidationError):
        ToolChoice(mode="tool")  # missing name
