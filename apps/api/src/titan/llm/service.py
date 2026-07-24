"""Public service interface of the `llm` module (TITAN-BCK-004, ADR-007).

Responsibility: provider-agnostic LLM gateway with config-driven provider
switching, resilience, cost metering, and budgets. Owns: llm_usage, budget_holds.

Only this interface may be imported by other modules; adapters, the config
loader, and the FastAPI router stay private to the package. The canonical
Pydantic DTOs — never provider payloads — cross this boundary.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.llm import budget
from titan.llm import repository as repo
from titan.llm.budget import GLOBAL_SCOPE_ID, BudgetManager, BudgetNotification, Notifier
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

# Re-exported so other DOMAIN modules can resolve the active model-policy
# profile without importing  directly — the boundary
# check (TITAN-BCK-001/004) admits only this module across the seam, and the
# runtime needs the profile to route agent turns (TITAN-FRT-036).
from titan.llm.settings_service import effective_profile
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
    budget_notifier: Notifier | None = None,
) -> LlmGateway:
    """Construct a gateway from settings. Pass ``client`` (e.g. one backed by an
    ``httpx.MockTransport``) to exercise adapters against recorded wire fixtures;
    otherwise the gateway owns a real client closed by :meth:`LlmGateway.aclose`.
    ``engine``/``redis`` enable usage accounting + budget enforcement; omit them
    (unit tests, zero-config local) and metering degrades to metrics only.

    ``budget_notifier`` is the composition-root callback the budget manager fires
    at the 80%/100% crossings (TITAN-SEC-082). The ``llm`` module must not import
    ``messaging``/``identity`` (module-independence contract), so the caller wires
    the notifier at the root and injects it here."""
    loader = ConfigLoader(Path(settings.models_config_path))
    pricebook = PriceBook(Path(settings.prices_config_path))
    http = client if client is not None else httpx.AsyncClient(timeout=_CLIENT_TIMEOUT)
    manager = BudgetManager(redis, engine, notify=budget_notifier)
    return LlmGateway(loader, http, pricebook=pricebook, engine=engine, redis=redis, budget=manager)


# --- budget-hold accessors (TITAN-OPS-021, wired into the state machines) ----


async def active_budget_hold(
    conn: AsyncConnection, *, project_id: uuid.UUID | None, task_id: uuid.UUID | None
) -> str | None:
    """The reason a project OR task is on budget hold in the CURRENT period, else
    None — across every TITAN-SEC-082 tier (task, project, project/day,
    global/month, local-time). The Phase-41 scheduler and pipeline consult this to
    PAUSE work at 100% budget (no new LLM calls) without losing the underlying
    resumable state (§ budget_hold). A windowed hold from a past day/month is a
    different scope string, so it is simply never queried — rollover resumes on
    its own."""
    candidates = budget.hold_candidates(project_id, task_id, datetime.now(UTC))
    row = await repo.get_hold(conn, candidates)
    return row.reason if row is not None else None


async def raise_budget(
    conn: AsyncConnection,
    *,
    scope: str,
    scope_id: uuid.UUID,
    redis: Redis | None = None,
    now: datetime | None = None,
) -> None:
    """Clear a persisted budget hold ("raise the budget") so paused work resumes
    exactly where it paused. ``scope`` is a tier KIND — 'task', 'project',
    'local_task', 'project_day', or 'org_month'; the windowed kinds are
    period-qualified against ``now`` (default UTC now) so the CURRENT day/month
    hold + counter are the ones cleared.

    Also resets the Redis rolling-spend counter for the scope when ``redis`` is
    given: clearing only the DB hold would leave the counter at cap, so the very
    next ``reserve()`` would re-breach and immediately re-hold — the resume would
    not stick for actual LLM work (TITAN-OPS-021)."""
    resolved = budget.period_scope(scope, now or datetime.now(UTC))
    await repo.clear_hold(conn, scope=resolved, scope_id=scope_id)
    if redis is not None:
        key = budget.spend_key(resolved, scope_id)
        # Reset the counter AND both once-per-crossing notify flags, so a resumed
        # budget re-notifies at the next 80%/100% crossing.
        await redis.delete(key, f"{key}:notified80", f"{key}:notified100")


async def raise_global_month_budget(
    conn: AsyncConnection, *, redis: Redis | None = None, now: datetime | None = None
) -> None:
    """Raise the deployment-global monthly ceiling (TITAN-SEC-082): clear the
    current month's ``org_month`` hold + reset its counter, so tasks paused on the
    global cap resume. TITAN is single-org, so this is the operator's "raise the
    org budget" action, keyed on the :data:`GLOBAL_SCOPE_ID` singleton."""
    await raise_budget(conn, scope="org_month", scope_id=GLOBAL_SCOPE_ID, redis=redis, now=now)


__all__ = [
    "GLOBAL_SCOPE_ID",
    "BudgetManager",
    "BudgetNotification",
    "Notifier",
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
    "active_budget_hold",
    "build_gateway",
    "compute_cost",
    "effective_profile",
    "is_transient_error",
    "parse_config",
    "raise_budget",
    "raise_global_month_budget",
]
