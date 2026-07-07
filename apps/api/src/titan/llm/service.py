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
from titan.llm.errors import (
    AllProvidersDownError,
    BudgetExceededError,
    ProviderNotConfiguredError,
    UnknownAliasError,
    UpstreamProviderError,
)
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


def is_transient_error(exc: BaseException) -> bool:
    """Whether a gateway failure is TRANSIENT — worth an automatic same-context
    retry (the runtime's recovery-ladder rung 1, TITAN-AGT-005) — versus terminal
    (a bad request, a config/credential error, or a hard budget stop, which must
    escalate immediately, not retry).

    Transient: the whole fallback chain was momentarily unavailable
    (:class:`AllProvidersDownError`, 503), an upstream timeout/5xx/rate-limit that
    surfaced after the gateway's own per-call retries, or a transport
    :class:`TimeoutError`/:class:`ConnectionError`. Terminal: budget hard-stop
    (402), unknown alias / not-configured (config, 4xx/500), and an upstream
    ``client_error``/``context_overflow`` (request-shaping, not provider health).
    """
    if isinstance(exc, TimeoutError | ConnectionError | AllProvidersDownError):
        return True
    if isinstance(exc, UpstreamProviderError):
        return exc.category in ("timeout", "server_error", "rate_limit")
    if isinstance(exc, BudgetExceededError | ProviderNotConfiguredError | UnknownAliasError):
        return False
    return False


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
    "is_transient_error",
    "parse_config",
]
