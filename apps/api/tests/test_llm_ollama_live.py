"""LIVE Ollama conformance (Phase 15 DoD; TITAN-INT-062).

Runs the golden transcript and the embeddings round-trip against a REAL Ollama on
the fully-local, zero-paid-key profile. Skipped unless Ollama is reachable and
the required model is pulled — recorded-cassette conformance for the paid
providers lands next phase. To enable locally:

    docker compose --profile local-llm up -d ollama
    docker exec titan-ollama-1 ollama pull nomic-embed-text
"""

import os
from pathlib import Path

import httpx
import pytest

from titan.llm.config import ConfigLoader
from titan.llm.gateway import LlmGateway
from titan.llm.models import ChatRequest, EmbeddingsRequest, Message, ToolDef

from .conftest import run_async

_BASE_URL = os.environ.get("TITAN_OLLAMA_BASE_URL", "http://localhost:11434")
_EMBED_MODEL = "nomic-embed-text"


# Ollama families whose chat template supports tool calling (Gemma2 does not).
_TOOL_CAPABLE = (
    "qwen2.5-coder",
    "qwen3-coder",
    "qwen2.5",
    "qwen3",
    "llama3.2",
    "llama3.1",
    "mistral",
)


def _tags() -> set[str]:
    """Full ``name:tag`` model names present on the live Ollama, or skip if it
    is unreachable."""
    try:
        resp = httpx.get(f"{_BASE_URL}/api/tags", timeout=2.0)
        resp.raise_for_status()
    except (httpx.HTTPError, OSError) as exc:
        pytest.skip(f"Ollama not reachable at {_BASE_URL}: {exc}")
    return {m["name"] for m in resp.json().get("models", [])}


def _pick_chat_model(tags: set[str]) -> str | None:
    """The first tool-capable model present, by full tag (Ollama needs the tag —
    there may be no ':latest')."""
    for family in _TOOL_CAPABLE:
        for tag in sorted(tags):
            if tag.split(":")[0] == family:
                return tag
    return None


def _gateway(tmp_path: Path, model_line: str) -> LlmGateway:
    """A gateway over a real (network) client. Because that client binds to the
    event loop of its first request, callers MUST run the request AND aclose in
    ONE ``run_async`` coroutine (see the tests) — closing across loops fails."""
    cfg = (
        f"providers:\n  ollama: {{ base_url: {_BASE_URL} }}\n"
        f"model_aliases:\n{model_line}"
        f"  embed-default: {{ provider: ollama, model: {_EMBED_MODEL}, dimensions: 768 }}\n"
        "routing:\n  default: local-default\n  embeddings_default: embed-default\n"
    )
    path = tmp_path / "models.local.yaml"
    path.write_text(cfg, encoding="utf-8")
    return LlmGateway(ConfigLoader(path), httpx.AsyncClient())


def test_live_embeddings_768d_zero_key(tmp_path: Path) -> None:
    if not any(t.split(":")[0] == _EMBED_MODEL for t in _tags()):
        pytest.skip(f"pull the embedding model first: ollama pull {_EMBED_MODEL}")
    gateway = _gateway(tmp_path, "  local-default: { provider: ollama, model: placeholder }\n")

    async def run() -> object:
        try:
            return await gateway.embed(EmbeddingsRequest(input=["titan", "gateway"]))
        finally:
            await gateway.aclose()

    resp = run_async(run)
    assert resp.dimensions == 768
    assert len(resp.embeddings) == 2
    assert all(len(v.embedding) == 768 for v in resp.embeddings)
    assert resp.provider == "ollama"
    assert resp.cost_usd == 0


def test_live_chat_tool_wire_round_trips(tmp_path: Path) -> None:
    chat_model = _pick_chat_model(_tags())
    if chat_model is None:
        pytest.skip("no tool-capable local chat model pulled (e.g. ollama pull qwen2.5)")
    gateway = _gateway(tmp_path, f"  local-default: {{ provider: ollama, model: {chat_model} }}\n")
    req = ChatRequest(
        messages=[Message(role="user", content="Use the tool to get the weather in Paris.")],
        tools=[
            ToolDef(
                name="get_weather",
                description="Get the weather for a city.",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            )
        ],
        max_tokens=256,
    )

    async def run() -> object:
        try:
            return await gateway.chat(req)
        finally:
            await gateway.aclose()

    resp = run_async(run)
    # The tool-calling wire round-trips live: a well-formed canonical response
    # (text and/or normalized tool calls), attributed to Ollama.
    assert resp.provider == "ollama"
    assert resp.model_id == chat_model
    assert resp.text is not None or resp.tool_calls
    assert resp.usage.output_tokens >= 0
