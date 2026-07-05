"""Gateway resilience tests (Phase 17; Vol 7 §8.3, TITAN-FND-053).

Circuit breaker + retry units, plus the chaos-fallback DoD at the response-
metadata level (no DB): a blackholed primary provider is recovered on the
fallback chain, tagged degraded. The audit-record half of the DoD is asserted in
``test_llm_budget.py`` (needs the DB). All wire traffic is an httpx.MockTransport.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from titan.llm.config import ConfigLoader
from titan.llm.errors import AllProvidersDownError, UpstreamProviderError
from titan.llm.gateway import LlmGateway
from titan.llm.models import ChatRequest, Message
from titan.llm.resilience import CircuitBreaker, RetryPolicy

from .conftest import run_async

# --- circuit breaker -------------------------------------------------------


def test_circuit_breaker_opens_then_half_opens() -> None:
    clock = {"t": 100.0}
    breaker = CircuitBreaker(threshold=5, window=30.0, cooldown=30.0, now=lambda: clock["t"])
    for _ in range(5):
        breaker.record_failure("anthropic")
    assert breaker.state("anthropic") == "open"
    assert not breaker.allow("anthropic")

    clock["t"] += 31.0  # cooldown elapsed → half-open probe admitted
    assert breaker.state("anthropic") == "half_open"
    assert breaker.allow("anthropic")
    breaker.record_success("anthropic")  # probe ok → closed
    assert breaker.state("anthropic") == "closed"


def test_circuit_breaker_prunes_stale_failures() -> None:
    clock = {"t": 0.0}
    breaker = CircuitBreaker(threshold=3, window=10.0, now=lambda: clock["t"])
    breaker.record_failure("p")
    breaker.record_failure("p")
    clock["t"] = 20.0  # first two failures age out of the window
    breaker.record_failure("p")
    assert breaker.allow("p")  # only 1 failure inside the window


def test_half_open_failure_reopens() -> None:
    clock = {"t": 0.0}
    breaker = CircuitBreaker(threshold=1, cooldown=10.0, now=lambda: clock["t"])
    breaker.record_failure("p")
    assert breaker.state("p") == "open"
    clock["t"] = 11.0
    assert breaker.state("p") == "half_open"
    breaker.record_failure("p")  # probe fails → re-open cooldown
    assert breaker.state("p") == "open"


def test_half_open_admits_single_probe() -> None:
    clock = {"t": 0.0}
    breaker = CircuitBreaker(threshold=1, cooldown=10.0, now=lambda: clock["t"])
    breaker.record_failure("p")
    clock["t"] = 11.0  # half-open
    assert breaker.allow("p")  # first caller is the probe
    assert not breaker.allow("p")  # concurrent callers wait for it to resolve
    breaker.record_success("p")
    assert breaker.allow("p")  # closed again


# --- retry -----------------------------------------------------------------


async def _noop_sleep(_seconds: float) -> None:
    return None


def test_retry_retries_retriable_then_raises() -> None:
    calls = {"n": 0}

    async def attempt() -> str:
        calls["n"] += 1
        raise UpstreamProviderError("p", "boom", category="server_error")

    policy = RetryPolicy(max_retries=2, sleep=_noop_sleep, rand=lambda: 0.0)
    with pytest.raises(UpstreamProviderError):
        run_async(lambda: policy.run(attempt))
    assert calls["n"] == 3  # initial + 2 retries


def test_retry_does_not_retry_non_retriable() -> None:
    calls = {"n": 0}

    async def attempt() -> str:
        calls["n"] += 1
        raise UpstreamProviderError("p", "429", category="rate_limit")

    policy = RetryPolicy(max_retries=3, sleep=_noop_sleep)
    with pytest.raises(UpstreamProviderError):
        run_async(lambda: policy.run(attempt))
    assert calls["n"] == 1  # rate_limit falls over immediately, no retry


# --- chaos fallback (response metadata) ------------------------------------

_CHAOS_CONFIG = """
providers:
  anthropic: { api_key: k, base_url: https://api.anthropic.com }
  ollama:    { base_url: http://ollama-test:11434 }
model_aliases:
  primary: { provider: anthropic, model: claude-sonnet-4-5, context_window: 200000 }
  local:   { provider: ollama, model: qwen3-coder:30b, context_window: 32768 }
routing:
  default: primary
  fallbacks:
    primary: [local]
"""

_OLLAMA_OK = {
    "choices": [
        {"message": {"role": "assistant", "content": "served by fallback"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


def _fast_gateway(
    tmp_path: Path, handler: httpx.MockTransport, cfg: str = _CHAOS_CONFIG
) -> LlmGateway:
    path = tmp_path / "models.yaml"
    path.write_text(cfg, encoding="utf-8")
    return LlmGateway(
        ConfigLoader(path),
        httpx.AsyncClient(transport=handler),
        retry=RetryPolicy(max_retries=2, sleep=_noop_sleep, rand=lambda: 0.0),
    )


def _chat(gateway: LlmGateway, text: str = "hi") -> Any:
    return run_async(
        lambda: gateway.chat(
            ChatRequest(messages=[Message(role="user", content=text)], max_tokens=64)
        )
    )


def test_blackholed_primary_recovers_on_fallback(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/messages"):
            return httpx.Response(503, text="upstream unavailable")  # blackholed
        return httpx.Response(200, json=_OLLAMA_OK)

    resp = _chat(_fast_gateway(tmp_path, httpx.MockTransport(handler)))
    assert resp.provider == "ollama"
    assert resp.requested_alias == "primary"
    assert resp.served_alias == "local"
    assert resp.degraded_model is True
    assert resp.text == "served by fallback"


def test_primary_dies_mid_conversation(tmp_path: Path) -> None:
    state = {"anthropic_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/messages"):
            state["anthropic_calls"] += 1
            if state["anthropic_calls"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "content": [{"type": "text", "text": "primary turn"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 5, "output_tokens": 3},
                    },
                )
            return httpx.Response(503, text="now down")  # blackholed after turn 1
        return httpx.Response(200, json=_OLLAMA_OK)

    gateway = _fast_gateway(tmp_path, httpx.MockTransport(handler))
    turn1 = _chat(gateway, "turn one")
    turn2 = _chat(gateway, "turn two")
    assert turn1.provider == "anthropic" and turn1.degraded_model is False
    assert turn2.provider == "ollama" and turn2.degraded_model is True


def test_rate_limit_falls_over_without_retrying_same_provider(tmp_path: Path) -> None:
    state = {"anthropic_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/messages"):
            state["anthropic_calls"] += 1
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json=_OLLAMA_OK)

    resp = _chat(_fast_gateway(tmp_path, httpx.MockTransport(handler)))
    assert resp.provider == "ollama"
    assert state["anthropic_calls"] == 1  # 429 not retried at the same provider


def test_all_providers_down_raises_503(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    with pytest.raises(AllProvidersDownError) as exc:
        _chat(_fast_gateway(tmp_path, httpx.MockTransport(handler)))
    assert exc.value.status == 503


_OVERFLOW_CONFIG = """
providers:
  anthropic: { api_key: k, base_url: https://api.anthropic.com }
model_aliases:
  small:   { provider: anthropic, model: model-small, context_window: 1000 }
  smaller: { provider: anthropic, model: model-smaller, context_window: 500 }
  bigger:  { provider: anthropic, model: model-bigger, context_window: 200000 }
routing:
  default: small
  fallbacks:
    small: [smaller, bigger]
"""


def test_context_overflow_only_falls_back_to_larger_context(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "model-bigger":
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "fit"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        return httpx.Response(400, text="prompt is too long: maximum context length exceeded")

    resp = _chat(_fast_gateway(tmp_path, httpx.MockTransport(handler), cfg=_OVERFLOW_CONFIG))
    assert resp.served_alias == "bigger"
    # 'smaller' (500 < 1000) must be SKIPPED — only larger-context aliases qualify.
    assert "model-smaller" not in seen
    assert seen == ["model-small", "model-bigger"]


def test_context_overflow_does_not_trip_circuit_breaker(tmp_path: Path) -> None:
    # A 400 "prompt too long" is a request-shape issue, not provider ill-health —
    # repeated overflows must NOT open the (healthy) provider's circuit.
    breaker = CircuitBreaker(threshold=5, window=30.0, cooldown=30.0)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="prompt is too long: maximum context length exceeded")

    path = tmp_path / "models.yaml"
    path.write_text(_CHAOS_CONFIG, encoding="utf-8")
    gateway = LlmGateway(
        ConfigLoader(path),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry=RetryPolicy(max_retries=1, sleep=_noop_sleep, rand=lambda: 0.0),
        breaker=breaker,
    )
    for _ in range(6):  # more than the 5-failure threshold
        with pytest.raises(Exception):  # noqa: B017 — overflow with no larger alias
            _chat(gateway)
    assert breaker.state("anthropic") == "closed"  # never opened by client-side 400s


def test_all_circuit_open_chain_is_503(tmp_path: Path) -> None:
    # Both anthropic candidates' circuit is open AND ollama fails → the whole
    # chain is unavailable → paused_provider (503), not a single-provider 502.
    breaker = CircuitBreaker(threshold=1, cooldown=999.0)
    breaker.record_failure("anthropic")  # open the shared anthropic circuit

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")  # ollama also down

    cfg = """
providers:
  anthropic: { api_key: k, base_url: https://api.anthropic.com }
  ollama:    { base_url: http://ollama-test:11434 }
model_aliases:
  a1:    { provider: anthropic, model: m1 }
  a2:    { provider: anthropic, model: m2 }
  local: { provider: ollama, model: q }
routing:
  default: a1
  fallbacks:
    a1: [a2, local]
"""
    path = tmp_path / "models.yaml"
    path.write_text(cfg, encoding="utf-8")
    gateway = LlmGateway(
        ConfigLoader(path),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry=RetryPolicy(max_retries=1, sleep=_noop_sleep, rand=lambda: 0.0),
        breaker=breaker,
    )
    with pytest.raises(AllProvidersDownError) as exc:
        _chat(gateway)
    assert exc.value.status == 503
