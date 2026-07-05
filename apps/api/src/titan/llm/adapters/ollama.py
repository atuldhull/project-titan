"""Ollama adapter over its OpenAI-compatible endpoints (Vol 7 §8.1, §8.2).

Ollama speaks the OpenAI Chat Completions and Embeddings wire formats, so the
canonical translation is shared with the OpenAI adapter (``_openai_compat``);
only the base URL, the ignore-me bearer token, and the (free) cost differ.
Ollama is local and free — cost is always zero (§8.4).
"""

from collections.abc import AsyncIterator

import httpx

from titan.llm.adapters import _openai_compat as compat
from titan.llm.adapters.base import ChatResult, EmbeddingsResult
from titan.llm.config import ProviderConfig
from titan.llm.errors import UpstreamProviderError
from titan.llm.models import ChatRequest, EmbeddingsRequest, StreamDelta
from titan.llm.routing import ResolvedModel

_DEFAULT_BASE_URL = "http://localhost:11434"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_PROVIDER = "ollama"


def _headers(provider: ProviderConfig) -> dict[str, str]:
    # Ollama ignores auth, but the OpenAI-compat layer accepts a bearer token —
    # forward one when configured so the same adapter fronts a keyed endpoint.
    return {
        "content-type": "application/json",
        "authorization": f"Bearer {provider.api_key or 'ollama'}",
    }


class OllamaAdapter:
    """Canonical ⇄ OpenAI-compatible (Ollama) translation, chat + embeddings."""

    async def chat(
        self,
        client: httpx.AsyncClient,
        *,
        req: ChatRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> ChatResult:
        base = provider.base_url or _DEFAULT_BASE_URL
        try:
            resp = await client.post(
                f"{base.rstrip('/')}/v1/chat/completions",
                headers=_headers(provider),
                json=compat.build_chat_payload(req, resolved, stream=False),
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise UpstreamProviderError(_PROVIDER, str(exc)) from exc
        if resp.status_code >= 400:
            raise UpstreamProviderError(_PROVIDER, f"{resp.status_code}: {resp.text[:500]}")
        return compat.parse_chat_response(resp.json(), provider=_PROVIDER)

    async def stream(
        self,
        client: httpx.AsyncClient,
        *,
        req: ChatRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> AsyncIterator[StreamDelta]:
        base = provider.base_url or _DEFAULT_BASE_URL
        try:
            async with client.stream(
                "POST",
                f"{base.rstrip('/')}/v1/chat/completions",
                headers=_headers(provider),
                json=compat.build_chat_payload(req, resolved, stream=True),
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode(errors="replace")[:500]
                    raise UpstreamProviderError(_PROVIDER, f"{resp.status_code}: {detail}")
                async for delta in compat.parse_chat_stream(resp):
                    yield delta
        except httpx.HTTPError as exc:
            raise UpstreamProviderError(_PROVIDER, str(exc)) from exc

    async def embed(
        self,
        client: httpx.AsyncClient,
        *,
        req: EmbeddingsRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> EmbeddingsResult:
        base = provider.base_url or _DEFAULT_BASE_URL
        try:
            resp = await client.post(
                f"{base.rstrip('/')}/v1/embeddings",
                headers=_headers(provider),
                json={"model": resolved.model_id, "input": req.input},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise UpstreamProviderError(_PROVIDER, str(exc)) from exc
        if resp.status_code >= 400:
            raise UpstreamProviderError(_PROVIDER, f"{resp.status_code}: {resp.text[:500]}")
        return compat.parse_embeddings(resp.json())


__all__ = ["OllamaAdapter"]
