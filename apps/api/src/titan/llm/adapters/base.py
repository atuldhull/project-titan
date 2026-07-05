"""Adapter protocols and their provider-neutral return types.

An adapter returns only what it can know — text, tool calls, finish reason, and
token usage. The gateway (:mod:`titan.llm.gateway`) stamps the cross-cutting
fields (provider, model_id, cost_usd, latency_ms) uniformly, so no adapter
duplicates that accounting.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from titan.llm.config import ProviderConfig
from titan.llm.models import (
    ChatRequest,
    EmbeddingsRequest,
    FinishReason,
    StreamDelta,
    ToolCall,
    Usage,
)
from titan.llm.routing import ResolvedModel


@dataclass
class ChatResult:
    """A provider's answer, still provider-neutral, before gateway stamping."""

    text: str | None
    tool_calls: list[ToolCall]
    finish_reason: FinishReason
    usage: Usage


@dataclass
class EmbeddingsResult:
    vectors: list[list[float]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


class ChatAdapter(Protocol):
    """Translates a canonical :class:`ChatRequest` to one provider and back."""

    async def chat(
        self,
        client: httpx.AsyncClient,
        *,
        req: ChatRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> ChatResult: ...

    def stream(
        self,
        client: httpx.AsyncClient,
        *,
        req: ChatRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> AsyncIterator[StreamDelta]: ...


class EmbeddingsAdapter(Protocol):
    async def embed(
        self,
        client: httpx.AsyncClient,
        *,
        req: EmbeddingsRequest,
        resolved: ResolvedModel,
        provider: ProviderConfig,
    ) -> EmbeddingsResult: ...


__all__ = [
    "ChatAdapter",
    "ChatResult",
    "EmbeddingsAdapter",
    "EmbeddingsResult",
]
