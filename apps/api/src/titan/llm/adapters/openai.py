"""OpenAI adapter — Chat Completions + Embeddings (Vol 7 §8.1).

Translation is the shared OpenAI dialect (``_openai_compat``); the OpenAI-specific
concerns handled here are required bearer auth and **strict-mode tool schemas**
(TITAN-INT-061), enabled per-alias via ``options.strict_tools`` in models.yaml —
config, not code. The Responses API is a superset of Chat Completions for our
canonical surface; Chat Completions is the tool-calling path we target at MVP.
"""

from collections.abc import AsyncIterator

import httpx

from titan.llm.adapters import _openai_compat as compat
from titan.llm.adapters.base import ChatResult, EmbeddingsResult
from titan.llm.config import ProviderConfig
from titan.llm.errors import (
    ProviderNotConfiguredError,
    upstream_from_httpx,
    upstream_from_response,
)
from titan.llm.models import ChatRequest, EmbeddingsRequest, StreamDelta
from titan.llm.routing import ResolvedModel

_DEFAULT_BASE_URL = "https://api.openai.com"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_PROVIDER = "openai"


def _headers(provider: ProviderConfig) -> dict[str, str]:
    if not provider.api_key:
        raise ProviderNotConfiguredError(
            "OpenAI requires OPENAI_API_KEY; none is configured "
            "(use the local-default alias for a zero-key profile)."
        )
    return {"authorization": f"Bearer {provider.api_key}", "content-type": "application/json"}


def _strict(resolved: ResolvedModel) -> bool:
    return bool(resolved.options.get("strict_tools", False))


class OpenAIAdapter:
    """Canonical ⇄ OpenAI Chat Completions / Embeddings translation."""

    async def chat(
        self,
        client: httpx.AsyncClient,
        *,
        req: ChatRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> ChatResult:
        base = provider.base_url or _DEFAULT_BASE_URL
        payload = compat.build_chat_payload(req, resolved, stream=False, strict=_strict(resolved))
        try:
            resp = await client.post(
                f"{base.rstrip('/')}/v1/chat/completions",
                headers=_headers(provider),
                json=payload,
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise upstream_from_httpx(_PROVIDER, exc) from exc
        if resp.status_code >= 400:
            raise upstream_from_response(_PROVIDER, resp, resp.text)
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
        payload = compat.build_chat_payload(req, resolved, stream=True, strict=_strict(resolved))
        try:
            async with client.stream(
                "POST",
                f"{base.rstrip('/')}/v1/chat/completions",
                headers=_headers(provider),
                json=payload,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode(errors="replace")[:500]
                    raise upstream_from_response(_PROVIDER, resp, detail)
                async for delta in compat.parse_chat_stream(resp):
                    yield delta
        except httpx.HTTPError as exc:
            raise upstream_from_httpx(_PROVIDER, exc) from exc

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
            raise upstream_from_httpx(_PROVIDER, exc) from exc
        if resp.status_code >= 400:
            raise upstream_from_response(_PROVIDER, resp, resp.text)
        return compat.parse_embeddings(resp.json())


__all__ = ["OpenAIAdapter"]
