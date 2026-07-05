"""Internal LLM gateway HTTP endpoint tests (Phase 15; Vol 7 §8.1).

Proves the ``/api/v1/llm/*`` surface is guarded by ``llm:invoke`` (the single
audited chokepoint, TITAN-INT-002) and returns the canonical response shapes.
The gateway on ``app.state`` is swapped for one backed by an ``httpx.MockTransport``
wire fixture, so no provider network call happens. Needs the disposable test DB
(schema marker) for the seeded RBAC roles + fakeredis.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.schema

_CONFIG = """
providers:
  anthropic: { api_key: test-key, base_url: https://api.anthropic.com }
  ollama:    { base_url: http://ollama-test:11434 }
model_aliases:
  coding-default: { provider: anthropic, model: claude-sonnet-4-5 }
  embed-default:  { provider: ollama,    model: nomic-embed-text, dimensions: 768 }
routing:
  default: coding-default
  embeddings_default: embed-default
"""


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/v1/messages"):
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hello from the gateway"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )
    if path.endswith("/v1/embeddings"):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.01] * 768, "index": i} for i in range(len(body["input"]))
                ],
                "usage": {"prompt_tokens": 4},
            },
        )
    return httpx.Response(404, json={})


def _install_gateway(api: dict[str, Any], tmp_path: Path) -> None:
    from titan.llm.config import ConfigLoader
    from titan.llm.gateway import LlmGateway

    path = tmp_path / "models.yaml"
    path.write_text(_CONFIG, encoding="utf-8")
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    api["client"].app.state.llm_gateway = LlmGateway(ConfigLoader(path), client)


_CHAT_BODY = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64}


def test_chat_requires_authentication(api: dict[str, Any], tmp_path: Path) -> None:
    _install_gateway(api, tmp_path)
    resp = api["client"].post("/api/v1/llm/chat", json=_CHAT_BODY)
    assert resp.status_code == 401


def test_chat_forbidden_without_permission(api: dict[str, Any], tmp_path: Path) -> None:
    _install_gateway(api, tmp_path)
    # viewer holds only *:read — not llm:invoke.
    resp = api["client"].post(
        "/api/v1/llm/chat", json=_CHAT_BODY, headers=api["token_for"]("viewer")
    )
    assert resp.status_code == 403


def test_chat_succeeds_for_owner(api: dict[str, Any], tmp_path: Path) -> None:
    _install_gateway(api, tmp_path)
    resp = api["client"].post(
        "/api/v1/llm/chat", json=_CHAT_BODY, headers=api["token_for"]("owner")
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["text"] == "hello from the gateway"
    assert payload["provider"] == "anthropic"
    assert payload["model_id"] == "claude-sonnet-4-5"
    assert payload["finish_reason"] == "stop"


def test_embeddings_round_trip(api: dict[str, Any], tmp_path: Path) -> None:
    _install_gateway(api, tmp_path)
    resp = api["client"].post(
        "/api/v1/llm/embeddings",
        json={"input": ["a", "b"]},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["dimensions"] == 768
    assert len(payload["embeddings"]) == 2
    assert payload["provider"] == "ollama"


def test_chat_validation_error_is_422(api: dict[str, Any], tmp_path: Path) -> None:
    _install_gateway(api, tmp_path)
    # max_tokens must be > 0.
    resp = api["client"].post(
        "/api/v1/llm/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 0},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
