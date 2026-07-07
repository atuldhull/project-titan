"""Public service interface of the `llm` module (TITAN-BCK-004, ADR-007).

Responsibility: provider-agnostic LLM gateway with config-driven provider
switching, resilience, cost metering, and budgets. Owns: llm_usage, budget_holds.

Only this interface may be imported by other modules; adapters, the config
loader, and the FastAPI router stay private to the package. The canonical
Pydantic DTOs — never provider payloads — cross this boundary.
"""

from pathlib import Path

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.llm.config import ConfigLoader, GatewayConfig, parse_config
from titan.llm.gateway import LlmGateway, compute_cost
from titan.llm.models import (
    ChatRequest,
    ChatResponse,
    ContentPart,
    EmbeddingsRequest,
    EmbeddingsResponse,
    EmbeddingVector,
    Message,
    RequestMetadata,
    StreamDelta,
    TextPart,
    ToolCall,
    ToolChoice,
    ToolDef,
    ToolResultPart,
    ToolUsePart,
    Usage,
)
from titan.llm.pricing import PriceBook
from titan.settings import Settings

# A generous default; each adapter also sets its own per-request timeout.
_CLIENT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def build_gateway(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    engine: AsyncEngine | None = None,
    redis: Redis | None = None,
) -> LlmGateway:
    """Construct a gateway from settings. Pass ``client`` (e.g. one backed by an
    ``httpx.MockTransport``) to exercise adapters against recorded wire fixtures;
    otherwise the gateway owns a real client closed by :meth:`LlmGateway.aclose`.
    ``engine``/``redis`` enable usage accounting + budget enforcement; omit them
    (unit tests, zero-config local) and metering degrades to metrics only."""
    loader = ConfigLoader(Path(settings.models_config_path))
    pricebook = PriceBook(Path(settings.prices_config_path))
    http = client if client is not None else httpx.AsyncClient(timeout=_CLIENT_TIMEOUT)
    return LlmGateway(loader, http, pricebook=pricebook, engine=engine, redis=redis)


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ConfigLoader",
    "ContentPart",
    "EmbeddingVector",
    "EmbeddingsRequest",
    "EmbeddingsResponse",
    "GatewayConfig",
    "LlmGateway",
    "Message",
    "RequestMetadata",
    "StreamDelta",
    "TextPart",
    "ToolCall",
    "ToolChoice",
    "ToolDef",
    "ToolResultPart",
    "ToolUsePart",
    "Usage",
    "build_gateway",
    "compute_cost",
    "parse_config",
]
