"""Alias-indirected model routing (Vol 7 §8.2, TITAN-INT-062).

Resolution order for chat: an explicit ``model_alias`` wins; else the caller's
``agent_id`` via ``routing.by_agent``; else ``routing.default``. Embeddings
resolve an explicit alias else ``routing.embeddings_default``. Fallback chains
are *exposed and validated* here so Phase 17 can execute them — this phase does
not fail over.
"""

from dataclasses import dataclass, field
from typing import Any

from titan.llm.config import GatewayConfig, Price, ProviderConfig
from titan.llm.errors import UnknownAliasError


@dataclass(frozen=True)
class ResolvedModel:
    """A routing decision: which concrete provider/model an alias points at."""

    alias: str
    provider: str
    model_id: str
    price: Price | None
    dimensions: int | None
    options: dict[str, Any] = field(default_factory=dict)


class ModelRouter:
    """Read-only view over a :class:`GatewayConfig` snapshot. Rebuilt cheaply per
    request so a hot-reloaded config takes effect immediately."""

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config

    def _resolve_alias(self, alias: str) -> ResolvedModel:
        spec = self._config.model_aliases.get(alias)
        if spec is None:
            raise UnknownAliasError(alias)
        return ResolvedModel(
            alias=alias,
            provider=spec.provider,
            model_id=spec.model,
            price=spec.price,
            dimensions=spec.dimensions,
            options=spec.options,
        )

    def resolve_chat(self, *, model_alias: str | None, agent_id: str | None) -> ResolvedModel:
        if model_alias:
            return self._resolve_alias(model_alias)
        if agent_id and agent_id in self._config.routing.by_agent:
            return self._resolve_alias(self._config.routing.by_agent[agent_id])
        return self._resolve_alias(self._config.routing.default)

    def resolve_embeddings(self, *, model_alias: str | None) -> ResolvedModel:
        if model_alias:
            return self._resolve_alias(model_alias)
        default = self._config.routing.embeddings_default
        if default is None:
            raise UnknownAliasError("<routing.embeddings_default unset>")
        return self._resolve_alias(default)

    def fallback_chain(self, alias: str) -> list[str]:
        """The configured fallback aliases for ``alias`` (validated; execution is
        Phase 17). Empty when none is configured."""
        return list(self._config.routing.fallbacks.get(alias, []))

    def provider_config(self, provider: str) -> ProviderConfig:
        return self._config.providers[provider]


__all__ = ["ModelRouter", "ResolvedModel"]
