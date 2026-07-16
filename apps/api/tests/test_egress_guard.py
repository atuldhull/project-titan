"""The offline-run egress guard (TITAN-FND-054, Phase 56). Pure — no DB.

The guard is an ALLOWLIST (loopback + the configured Ollama host); anything else
is blocked and recorded, so it genuinely proves "offline" rather than dodging a
hand-maintained denylist of known paid hosts."""

import httpx
import pytest

from tests.conftest import run_async
from titan.benchmark.egress_guard import EgressBlocked, EgressGuard, guarded_client


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def test_blocks_any_off_allowlist_host_including_non_paid_proxies() -> None:
    async def scenario() -> None:
        guard = EgressGuard(inner=httpx.MockTransport(_ok))
        client = guarded_client(guard)
        try:
            for url in (
                "https://api.anthropic.com/v1/messages",
                "https://api.openai.com/v1/chat/completions",
                "https://generativelanguage.googleapis.com/v1/models",
                # An allowlist (not a 4-host denylist) also blocks these:
                "https://myresource.openai.azure.com/openai/deployments/x",
                "https://openrouter.ai/api/v1/chat/completions",
            ):
                with pytest.raises(EgressBlocked):
                    await client.post(url, json={})
        finally:
            await client.aclose()
        assert len(guard.blocked) == 5
        assert not guard.clean

    run_async(scenario)


def test_allows_and_records_loopback_and_configured_ollama() -> None:
    async def scenario() -> None:
        guard = EgressGuard(inner=httpx.MockTransport(_ok), ollama_base_url="http://ollama:11434")
        client = guarded_client(guard)
        try:
            assert (
                await client.post("http://localhost:11434/api/chat", json={})
            ).status_code == 200
            await client.get("http://ollama:11434/api/tags")  # the compose hostname
            await client.get("http://127.0.0.1:11434/api/tags")
        finally:
            await client.aclose()
        assert guard.blocked == []
        assert guard.clean
        assert guard.hosts_contacted == {"localhost", "ollama", "127.0.0.1"}

    run_async(scenario)
