"""Four-backend conformance matrix (Phase 16; TITAN-INT-060/061).

The one golden transcript (``tests/_transcript.py``) runs against Anthropic,
OpenAI, Google, and Ollama — each served by its committed cassette, keyless and
offline. The backend under test is chosen by swapping ``routing.default`` in
models.yaml ALONE; the test body is identical across legs. Every backend must
round-trip to the SAME canonical transcript, proving config-only provider
switching and cross-provider tool-call normalization.
"""

from pathlib import Path

import pytest

from titan.llm.config import ConfigLoader
from titan.llm.gateway import LlmGateway

from . import _transcript as T
from ._cassettes import cassette_client

# One config with ALL four providers; only routing.default changes per leg.
_BASE_CONFIG = """
providers:
  anthropic: {{ api_key: test-key, base_url: https://api.anthropic.com }}
  openai:    {{ api_key: test-key, base_url: https://api.openai.com }}
  google:    {{ api_key: test-key, base_url: https://generativelanguage.googleapis.com }}
  ollama:    {{ base_url: http://ollama-test:11434 }}
model_aliases:
  anthropic-alias: {{ provider: anthropic, model: claude-sonnet-4-5 }}
  openai-alias:    {{ provider: openai, model: gpt-4.1, options: {{ strict_tools: true }} }}
  google-alias:    {{ provider: google, model: gemini-2.5-flash }}
  ollama-alias:    {{ provider: ollama, model: qwen3-coder:30b }}
routing:
  default: {default_alias}
"""

# (provider, alias, model_id) — the ONLY thing that varies between matrix legs.
LEGS = [
    ("anthropic", "anthropic-alias", "claude-sonnet-4-5"),
    ("openai", "openai-alias", "gpt-4.1"),
    ("google", "google-alias", "gemini-2.5-flash"),
    ("ollama", "ollama-alias", "qwen3-coder:30b"),
]


def _config_path(tmp_path: Path, default_alias: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(_BASE_CONFIG.format(default_alias=default_alias), encoding="utf-8")
    return path


def _gateway(tmp_path: Path, default_alias: str, cassette: str) -> LlmGateway:
    return LlmGateway(
        ConfigLoader(_config_path(tmp_path, default_alias)), cassette_client(cassette)
    )


@pytest.mark.parametrize("provider,alias,model_id", LEGS)
def test_golden_transcript_conformance(
    tmp_path: Path, provider: str, alias: str, model_id: str
) -> None:
    gateway = _gateway(tmp_path, alias, f"{provider}.transcript")
    r1, r2 = T.run_golden_transcript(gateway)
    T.assert_golden_canonical(r1, r2, provider=provider, model_id=model_id)


@pytest.mark.parametrize("provider,alias,model_id", LEGS)
def test_streaming_conformance(tmp_path: Path, provider: str, alias: str, model_id: str) -> None:
    gateway = _gateway(tmp_path, alias, f"{provider}.stream")
    deltas = T.collect_stream(gateway)
    T.assert_golden_stream(deltas)


def test_backend_selected_by_config_swap_only(tmp_path: Path) -> None:
    """The DoD proof: identical code + identical request, only routing.default
    edited, routes to a different provider each leg — no code change between."""
    observed: dict[str, tuple[str, str]] = {}
    for provider, alias, _model_id in LEGS:
        gateway = _gateway(tmp_path, alias, f"{provider}.transcript")
        r1, _ = T.run_golden_transcript(gateway)
        observed[provider] = (r1.provider, r1.model_id)
    assert observed == {p: (p, m) for p, _, m in LEGS}
