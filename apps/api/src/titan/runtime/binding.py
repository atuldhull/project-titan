"""The model-policy → gateway binding (Vol 2 §8, TITAN-AGT-021/022).

Two configs describe an agent's model, on purpose, at two layers:

- ``config/model-policy.yaml`` (Phase 28) is the *agent-facing policy*: which
  **tier** (reasoning/standard/fast) and **profile** (cloud vs. the offline
  ``local-free`` Ollama profile) each agent's turns route to, plus the sampling
  temperature. It is the single knob operators turn to switch an agent's model
  (TITAN-AGT-021) and to run the whole roster offline (TITAN-AGT-007).
- ``config/models.yaml`` (Phase 15) is the *gateway/infra binding*: the concrete
  alias → provider/model wiring, pricing, fallback chains, budgets, and the
  provider adapters. The LLM never leaves this module (TITAN-AGT-022).

This binding is the seam between them: it resolves the agent's
:class:`~titan.agents.service.ModelSelection` via model-policy, maps its
resolved **provider** (and tier, to disambiguate Anthropic) to a gateway model
*alias*, and builds the canonical :class:`~titan.llm.service.ChatRequest` — so the
runtime "calls the LLM through the gateway routed by model_policy" without ever
importing a provider SDK.

The provider is the load-bearing switch: editing an agent's (or the Critic pin's)
provider in model-policy.yaml re-selects the alias here, so the config is never
inert — satisfying TITAN-AGT-021. Switching the profile to ``local-free`` (or a
Critic pin to Ollama) resolves the provider to Ollama → the local alias, which the
gateway serves offline (traces show ``gen_ai.system = ollama``), satisfying
TITAN-AGT-007. models.yaml owns the exact pinned model + price + fallback behind
each alias; that split is intentional (pricing needs a pinned model, not a rolling
``…-latest`` pointer).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from titan.agents.service import ModelPolicyConfig, ModelSelection
from titan.llm.service import ChatRequest, ChatResponse, Message, RequestMetadata, ToolDef


class ModelBindingError(RuntimeError):
    """A model-policy selection resolves to a provider the gateway has no alias
    for — a config mismatch between model-policy.yaml and models.yaml. Raised
    rather than silently mis-routing."""


# The gateway aliases (config/models.yaml) a model-policy selection resolves to.
# The RESOLVED PROVIDER is the load-bearing switch (TITAN-AGT-021): editing an
# agent's provider in model-policy.yaml re-selects the alias here, so it is never
# inert. Anthropic spans two aliases so the tier disambiguates it; the other
# providers each have one alias today. models.yaml owns the exact pinned model +
# price + fallback behind each alias — that split is deliberate (pricing needs a
# pinned model, not a rolling "…-latest" pointer).
ANTHROPIC_TIER_ALIAS: Mapping[str, str] = {
    "reasoning": "reasoning-large",
    "standard": "coding-default",
    "fast": "coding-default",
}
PROVIDER_ALIAS: Mapping[str, str] = {
    "openai": "openai-default",
    "google": "fast-cheap",
    "ollama": "local-default",
}


class LlmClient(Protocol):
    """The one gateway method the runtime needs. :class:`LlmGateway` satisfies
    this structurally; a scripted double satisfies it in tests — either way the
    LLM is reached only through this boundary."""

    async def chat(self, req: ChatRequest) -> ChatResponse: ...


@dataclass(frozen=True)
class GatewayBinding:
    """Resolves an agent to a gateway :class:`ChatRequest` via model-policy."""

    policy: ModelPolicyConfig
    profile: str | None = None
    anthropic_tier_alias: Mapping[str, str] = field(
        default_factory=lambda: dict(ANTHROPIC_TIER_ALIAS)
    )
    provider_alias: Mapping[str, str] = field(default_factory=lambda: dict(PROVIDER_ALIAS))

    def selection(self, agent_id: str) -> ModelSelection:
        """The model-policy decision for this agent this turn (tier, provider,
        model, temperature) — honouring the active profile and the Critic pin."""
        return self.policy.resolve(agent_id, profile=self.profile)

    def alias_for(self, selection: ModelSelection) -> str:
        """Map a resolved selection to the gateway alias that serves it, keyed on
        the resolved PROVIDER (so model-policy's provider actually switches routing
        — TITAN-AGT-021). Anthropic is disambiguated by tier; other providers map
        by provider alone. An unmapped provider is a loud config error."""
        if selection.provider == "anthropic":
            return self.anthropic_tier_alias.get(selection.tier, "coding-default")
        alias = self.provider_alias.get(selection.provider)
        if alias is None:
            raise ModelBindingError(
                f"model-policy resolved provider '{selection.provider}' has no gateway "
                f"alias in config/models.yaml"
            )
        return alias

    def build_request(
        self,
        agent_id: str,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolDef],
        max_output_tokens: int,
        project_id: str,
        task_id: str | None,
    ) -> ChatRequest:
        """Build the canonical chat request for one agent turn. The system prompt
        leads; ``tools`` is the GRANTED-only tool list (never the full registry);
        ``metadata.agent_id`` buckets metrics/usage to the agent role."""
        selection = self.selection(agent_id)
        return ChatRequest(
            model_alias=self.alias_for(selection),
            messages=[Message(role="system", content=system), *messages],
            tools=tools or None,
            temperature=selection.temperature,
            max_tokens=max_output_tokens,
            metadata=RequestMetadata(
                agent_id=f"agent.{agent_id}", task_id=task_id, project_id=project_id
            ),
        )


__all__ = [
    "ANTHROPIC_TIER_ALIAS",
    "PROVIDER_ALIAS",
    "GatewayBinding",
    "LlmClient",
    "ModelBindingError",
]
