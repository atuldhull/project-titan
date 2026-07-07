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

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.agents.service import (
    build_prompt_composer,
    load_model_policy,
    load_roster,
)
from titan.events.service import QueueBus
from titan.llm.service import build_gateway
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
    MemoryPacket,
    MemoryPacketSections,
    MemoryProvider,
    MemoryRequest,
    NullMemoryProvider,
)
from titan.runtime.output import (
    AgentTurnOutput,
    OutboundMessage,
    TurnOutputError,
    parse_turn_output,
)
from titan.runtime.turn import (
    AgentRuntime,
    ToolCallRecord,
    TurnOutcome,
    TurnResult,
    run_turn,
)
from titan.runtime.worker import RuntimeWorker
from titan.settings import Settings
from titan.tools.service import ToolBus, build_bus, build_registry


def build_runtime(
    settings: Settings,
    engine: AsyncEngine,
    *,
    redis: Redis | None = None,
    llm: LlmClient | None = None,
    memory: MemoryProvider | None = None,
    tool_bus: ToolBus | None = None,
) -> AgentRuntime:
    """Wire the shared Agent Runtime from settings + live infrastructure.

    Loads the cached roster (Phase 26), the prompt composer (Phase 28), and the
    model-policy binding (Phase 28); builds the Tool Registry + Tool Bus
    (Phase 18) and the LLM gateway (Phase 15). Pass ``llm`` to substitute a
    scripted/mock provider (tests) and ``memory`` to inject real retrieval when
    the memory phases land. The caller owns the gateway's lifetime: when this
    builds it, close it with ``runtime.llm.aclose()`` on shutdown.
    """
    registry = load_roster(settings)
    composer = build_prompt_composer(settings)
    policy = load_model_policy(settings)
    binding = GatewayBinding(policy, settings.model_policy_profile)
    tool_registry = build_registry()
    bus = (
        tool_bus if tool_bus is not None else build_bus(engine, registry=tool_registry, redis=redis)
    )
    gateway: LlmClient = (
        llm if llm is not None else build_gateway(settings, engine=engine, redis=redis)
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
    "PROVIDER_ALIAS",
    "AgentRuntime",
    "AgentTurnOutput",
    "Citation",
    "GatewayBinding",
    "LlmClient",
    "ModelBindingError",
    "MemoryPacket",
    "MemoryPacketSections",
    "MemoryProvider",
    "MemoryRequest",
    "NullMemoryProvider",
    "OutboundMessage",
    "RuntimeWorker",
    "SecurityEvent",
    "ToolCallRecord",
    "TurnOutcome",
    "TurnOutputError",
    "TurnResult",
    "build_runtime",
    "build_worker",
    "granted_scopes",
    "granted_tool_defs",
    "is_granted",
    "parse_turn_output",
    "run_turn",
]
