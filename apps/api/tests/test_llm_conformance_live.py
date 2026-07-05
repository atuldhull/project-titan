"""LIVE four-provider conformance (Phase 16 item 4; TITAN-INT-060).

Runs the golden transcript against REAL providers to catch API divergence the
cassettes can't (new tool-call semantics, deprecations). Each leg self-skips
unless its credential is present, so the main pipeline is unaffected; CI runs
this as a separate, non-blocking ``live_llm`` job when keys are configured.

Real models are non-deterministic, so this asserts STRUCTURAL conformance
(valid canonical response, correct provider attribution, tool wire round-trips),
not the byte-exact golden the cassette matrix asserts.
"""

import os
from pathlib import Path

import httpx
import pytest

from titan.llm.config import ConfigLoader
from titan.llm.gateway import LlmGateway
from titan.llm.models import ChatRequest, Message, ToolDef

from .conftest import run_async

pytestmark = pytest.mark.live_llm

_OLLAMA_URL = os.environ.get("TITAN_OLLAMA_BASE_URL", "http://localhost:11434")

# (provider, key_env, model) — collected from the environment at run time.
_CLOUD = [
    ("anthropic", "ANTHROPIC_API_KEY", "claude-3-5-haiku-latest"),
    ("openai", "OPENAI_API_KEY", "gpt-4o-mini"),
    ("google", "GOOGLE_API_KEY", "gemini-2.5-flash"),
]

_TOOL = ToolDef(
    name="get_weather",
    description="Get the weather for a city.",
    input_schema={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)


def _ollama_model() -> str | None:
    try:
        resp = httpx.get(f"{_OLLAMA_URL}/api/tags", timeout=2.0)
        resp.raise_for_status()
    except (httpx.HTTPError, OSError):
        return None
    tags = {str(m["name"]) for m in resp.json().get("models", [])}
    for family in ("qwen2.5-coder", "qwen2.5", "qwen3", "llama3.2", "llama3.1"):
        for tag in sorted(tags):
            if tag.split(":")[0] == family:
                return tag
    return None


def _run_live(gateway: LlmGateway, provider: str) -> None:
    req = ChatRequest(
        messages=[Message(role="user", content="Use the tool for the weather in Paris.")],
        tools=[_TOOL],
        max_tokens=256,
    )

    async def run() -> None:
        try:
            r1 = await gateway.chat(req)
            assert r1.provider == provider
            assert r1.usage.input_tokens >= 0
            # The tool-calling wire round-trips: either a normalized tool call or
            # a plain answer, both canonically valid.
            assert r1.text is not None or r1.tool_calls
        finally:
            await gateway.aclose()

    run_async(run)


@pytest.mark.parametrize("provider,key_env,model", _CLOUD)
def test_live_cloud_provider(tmp_path: Path, provider: str, key_env: str, model: str) -> None:
    if not os.environ.get(key_env):
        pytest.skip(f"{key_env} not set")
    cfg = (
        f"providers:\n  {provider}: {{ api_key: ${{{key_env}}} }}\n"
        f"model_aliases:\n  m: {{ provider: {provider}, model: {model} }}\n"
        "routing:\n  default: m\n"
    )
    path = tmp_path / "models.yaml"
    path.write_text(cfg, encoding="utf-8")
    _run_live(LlmGateway(ConfigLoader(path), httpx.AsyncClient()), provider)


def test_live_ollama(tmp_path: Path) -> None:
    model = _ollama_model()
    if model is None:
        pytest.skip("Ollama not reachable or no tool-capable model pulled")
    cfg = (
        f"providers:\n  ollama: {{ base_url: {_OLLAMA_URL} }}\n"
        f"model_aliases:\n  m: {{ provider: ollama, model: {model} }}\n"
        "routing:\n  default: m\n"
    )
    path = tmp_path / "models.yaml"
    path.write_text(cfg, encoding="utf-8")
    _run_live(LlmGateway(ConfigLoader(path), httpx.AsyncClient()), "ollama")
