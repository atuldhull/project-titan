"""Public service interface of the `runtime` module (TITAN-BCK-004).

Responsibility: the shared Agent Runtime — the stateless turn loop that
hydrates a role, calls the LLM gateway routed by model-policy, validates
structured output, enforces the ``tool_grants`` allow-list, executes granted
tools through the Tool Bus, and emits the resulting messages via the outbox
(Vol 2 §8, TITAN-AGT-002/004/023/024).

Only this interface may be imported by other modules; the turn internals, the
binding, and the grant enforcement stay private to the package. The runtime
composes the OTHER modules strictly through their ``.service`` boundaries
(agents, messaging, llm, tools) and never imports a provider SDK.
"""

import uuid
from pathlib import Path
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.agents.service import (
    build_prompt_composer,
    load_model_policy,
    load_roster,
)
from titan.conflict.service import vetoing_roles as _vetoing_roles
from titan.events.service import QueueBus
from titan.llm.service import ConfigLoader, build_gateway, effective_profile
from titan.memory import service as memory_service
from titan.runtime.binding import (
    ANTHROPIC_TIER_ALIAS,
    PROVIDER_ALIAS,
    GatewayBinding,
    LlmClient,
    ModelBindingError,
)
from titan.runtime.grants import (
    AGENT_ROLE,
    SecurityEvent,
    granted_scopes,
    granted_tool_defs,
    is_granted,
)
from titan.runtime.memory import (
    Citation,
    KgEdgeEvent,
    KgEventSink,
    MemoryPacket,
    MemoryPacketSections,
    MemoryProvider,
    MemoryRequest,
    NoopKgSink,
    NullMemoryProvider,
    PacketMemoryProvider,
    WorkingMemoryCoordinator,
)
from titan.runtime.metrics import declare_kpis
from titan.runtime.output import (
    AgentTurnOutput,
    OutboundMessage,
    TurnOutputError,
    parse_turn_output,
)
from titan.runtime.recovery import (
    LadderConfig,
    build_escalation,
    is_blocked_escalation,
    is_transient,
    turn_operation_label,
)
from titan.runtime.turn import (
    AgentRuntime,
    FailureReason,
    ToolCallRecord,
    TurnOutcome,
    TurnResult,
    run_turn,
)
from titan.runtime.watchdog import (
    DISCUSSION_MODERATOR,
    SweepReport,
    Watchdog,
    WatchdogConfig,
    build_watchdog,
    load_watchdog_config,
    set_watchdog_config,
)
from titan.runtime.worker import RuntimeWorker
from titan.settings import Settings
from titan.tools.service import ToolBus, build_bus, build_registry

logger = structlog.get_logger("titan.runtime")


def build_runtime(
    settings: Settings,
    engine: AsyncEngine,
    *,
    redis: Redis | None = None,
    llm: LlmClient | None = None,
    memory: MemoryProvider | None = None,
    tool_bus: ToolBus | None = None,
    http: Any | None = None,
    reranker: Any | None = None,
) -> AgentRuntime:
    """Wire the shared Agent Runtime from settings + live infrastructure.

    Loads the cached roster (Phase 26), the prompt composer (Phase 28), and the
    model-policy binding (Phase 28); builds the Tool Registry + Tool Bus
    (Phase 18) and the LLM gateway (Phase 15). Pass ``llm`` to substitute a
    scripted/mock provider (tests). When ``redis`` + ``http`` (Qdrant) +
    ``reranker`` are supplied, the runtime gets the REAL memory wiring
    (Phase 38, §13): the Phase-36 packet provider and the working-memory
    coordinator; otherwise the null provider keeps turns valid with empty
    packets. The caller owns the gateway's lifetime: when this builds it,
    close it with ``runtime.llm.aclose()`` on shutdown.
    """
    registry = load_roster(settings)
    # Publish each agent's declared KPIs to Prometheus so the series exist for
    # dashboards from boot (§2.2; domain phases fill in real values).
    for definition in registry.all():
        declare_kpis(f"agent.{definition.agent_id}", definition.kpis)
    composer = build_prompt_composer(settings)
    policy = load_model_policy(settings)
    # alias -> context window for the §8.1 budget allocator; a missing/broken
    # models.yaml degrades to the allocator's default window, never a crash.
    windows: dict[str, int] = {}
    # The profile Settings has written, else the process setting. Resolved here
    # (per runtime build) rather than frozen at import, so a Settings change
    # re-points agent turns without a restart.
    #
    # This is the load-bearing switch, and it is NOT `routing.default`: an agent
    # turn never falls through to it, because `GatewayBinding.build_request`
    # always passes an explicit `model_alias` resolved from model-policy. So the
    # profile, not the default alias, is what TITAN-FRT-036's "the next agent
    # action logs the new provider" actually depends on.
    profile = settings.model_policy_profile
    try:
        loader = ConfigLoader(Path(settings.models_config_path))
        config = loader.get()
        windows = {
            alias: model.context_window
            for alias, model in config.model_aliases.items()
            if model.context_window
        }
        profile = effective_profile(loader, settings.model_policy_profile)
    except Exception:
        logger.warning("runtime.context_windows_unavailable", exc_info=True)
    binding = GatewayBinding(policy, profile, context_windows=windows)
    tool_registry = build_registry()
    bus = (
        tool_bus if tool_bus is not None else build_bus(engine, registry=tool_registry, redis=redis)
    )
    gateway: LlmClient = (
        llm if llm is not None else build_gateway(settings, engine=engine, redis=redis)
    )
    if memory is None and redis is not None and http is not None and reranker is not None:
        memory = PacketMemoryProvider(
            engine, redis, memory_service.QdrantIndex(http), gateway, reranker
        )
    coordinator: WorkingMemoryCoordinator | None = None
    if redis is not None:
        # The summary fold routes to the cheapest configured chat model
        # (utility tier — TITAN-BRN-017); unreadable config -> gateway default.
        alias: str | None = None
        try:
            alias = memory_service.cheapest_chat_alias(settings.models_config_path)
        except Exception:
            logger.warning("runtime.summary_alias_unavailable")
        coordinator = WorkingMemoryCoordinator(
            engine,
            memory_service.WorkingMemory(redis),
            memory_service.SummaryFolder(gateway, alias),
            # Phase 39: the §13 step-11 KG hook now writes real graph nodes/edges
            # (best-effort, idempotent) instead of the Phase-38 typed no-op.
            memory_service.GraphKgSink(engine),
        )
    return AgentRuntime(
        engine=engine,
        registry=registry,
        composer=composer,
        tool_registry=tool_registry,
        tool_bus=bus,
        llm=gateway,
        binding=binding,
        memory=memory if memory is not None else NullMemoryProvider(),
        coordinator=coordinator,
        # Data-driven from the roster: exactly the roles whose AgentDefinition holds
        # a blocking veto (Security by default, §7.4) — never a hard-coded id.
        vetoing_roles=_vetoing_roles(registry),
    )


def build_worker(
    runtime: AgentRuntime,
    bus: QueueBus,
    project_id: uuid.UUID,
    agent_id: str,
    *,
    consumer: str = "runtime-1",
) -> RuntimeWorker:
    """One worker serving ``agent_id``'s inbox on the given queue bus."""
    return RuntimeWorker(runtime, bus, project_id, agent_id, consumer)


__all__ = [
    "AGENT_ROLE",
    "ANTHROPIC_TIER_ALIAS",
    "DISCUSSION_MODERATOR",
    "PROVIDER_ALIAS",
    "AgentRuntime",
    "AgentTurnOutput",
    "Citation",
    "FailureReason",
    "GatewayBinding",
    "LadderConfig",
    "LlmClient",
    "ModelBindingError",
    "MemoryPacket",
    "MemoryPacketSections",
    "MemoryProvider",
    "MemoryRequest",
    "NoopKgSink",
    "KgEventSink",
    "KgEdgeEvent",
    "NullMemoryProvider",
    "WorkingMemoryCoordinator",
    "PacketMemoryProvider",
    "OutboundMessage",
    "RuntimeWorker",
    "SecurityEvent",
    "SweepReport",
    "ToolCallRecord",
    "TurnOutcome",
    "TurnOutputError",
    "TurnResult",
    "Watchdog",
    "WatchdogConfig",
    "build_escalation",
    "build_runtime",
    "build_watchdog",
    "build_worker",
    "granted_scopes",
    "granted_tool_defs",
    "is_blocked_escalation",
    "is_granted",
    "is_transient",
    "load_watchdog_config",
    "parse_turn_output",
    "run_turn",
    "set_watchdog_config",
    "turn_operation_label",
]
