"""``config/models.yaml`` loader and schema (Vol 7 §8.2, TITAN-INT-060/062).

The gateway is config-driven: providers, model aliases, and per-agent routing
live in YAML, so adding or switching a provider/model is a configuration change
only — never code (TITAN-INT-060). Secrets are referenced as ``${ENV_VAR}`` and
resolved from the environment at load time; a missing var resolves to empty so
the fully-local, zero-paid-key profile loads and validates cleanly (an adapter
that actually needs a key fails later, at call time, with a clear error).

The file is hot-reloadable: :class:`ConfigLoader` re-reads it when its mtime
changes, so operators can retune routing without restarting the process.
"""

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = structlog.get_logger("titan.llm.config")

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# ``model``/``model_aliases`` names collide with Pydantic's protected namespace.
_ALLOW_MODEL_FIELDS = ConfigDict(protected_namespaces=(), extra="forbid")


class ProviderConfig(BaseModel):
    """Connection details for one provider. ``api_key`` empty is legal (Ollama,
    or a not-yet-configured paid provider on the local profile)."""

    model_config = ConfigDict(extra="forbid")
    api_key: str | None = None
    base_url: str | None = None


class Price(BaseModel):
    """Per-million-token prices for cost accounting (§8.4). Absent ⇒ free."""

    model_config = ConfigDict(extra="forbid")
    input_per_mtok: Decimal = Decimal(0)
    output_per_mtok: Decimal = Decimal(0)


class ModelAlias(BaseModel):
    """An indirection from a stable alias to a concrete provider+model. Callers
    and routing reference aliases, never raw model ids (TITAN-INT-062)."""

    model_config = _ALLOW_MODEL_FIELDS
    provider: str
    model: str
    dimensions: int | None = None  # embedding aliases declare their vector width
    price: Price | None = None
    # Max context tokens — a context-overflow fallback only steps UP to a
    # larger-context alias (Vol 7 §8.3). None ⇒ unknown (treated as smallest).
    context_window: int | None = None
    # Per-alias provider options (config-driven quirks, e.g. {strict_tools: true}
    # for OpenAI). Adapters read what they understand; unknown keys are ignored.
    options: dict[str, Any] = Field(default_factory=dict)


class Routing(BaseModel):
    """Alias-indirected routing (§8.2). ``fallbacks`` is validated now; its
    execution lands in Phase 17 (TITAN-INT-063)."""

    model_config = ConfigDict(extra="forbid")
    default: str
    embeddings_default: str | None = None
    by_agent: dict[str, str] = Field(default_factory=dict)
    fallbacks: dict[str, list[str]] = Field(default_factory=dict)


class Budgets(BaseModel):
    """Budget caps (§8.4); enforcement lands in Phase 18 (TITAN-INT-064)."""

    model_config = ConfigDict(extra="forbid")
    per_project_usd: Decimal | None = None
    per_task_usd: Decimal | None = None
    action_on_exceeded: str | None = None


class GatewayConfig(BaseModel):
    """The fully-parsed, cross-referentially-valid ``models.yaml``."""

    model_config = _ALLOW_MODEL_FIELDS
    providers: dict[str, ProviderConfig]
    model_aliases: dict[str, ModelAlias]
    routing: Routing
    budgets: Budgets | None = None

    @model_validator(mode="after")
    def _validate_references(self) -> "GatewayConfig":
        aliases = set(self.model_aliases)
        for name, alias in self.model_aliases.items():
            if alias.provider not in self.providers:
                raise ValueError(
                    f"model_alias '{name}' references unknown provider '{alias.provider}'"
                )
        if self.routing.default not in aliases:
            raise ValueError(f"routing.default '{self.routing.default}' is not a defined alias")
        if self.routing.embeddings_default is not None and (
            self.routing.embeddings_default not in aliases
        ):
            raise ValueError(
                f"routing.embeddings_default '{self.routing.embeddings_default}' "
                "is not a defined alias"
            )
        for agent, alias_name in self.routing.by_agent.items():
            if alias_name not in aliases:
                raise ValueError(
                    f"routing.by_agent['{agent}'] references unknown alias '{alias_name}'"
                )
        for primary, chain in self.routing.fallbacks.items():
            for ref in (primary, *chain):
                if ref not in aliases:
                    raise ValueError(f"routing.fallbacks references unknown alias '{ref}'")
        return self


def _resolve_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` in every string. A missing var expands to
    the empty string so the zero-key local profile still loads."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def parse_config(raw: str) -> GatewayConfig:
    """Parse+validate a YAML document (with ``${ENV}`` already expandable)."""
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("models.yaml must be a mapping at the top level")
    return GatewayConfig.model_validate(_resolve_env(data))


class ConfigLoader:
    """Loads and caches ``models.yaml``, reloading on mtime change (§8.2 hot
    reload). ``get()`` is intentionally synchronous — a stat + small read is
    sub-millisecond and must not be awaited from adapter hot paths."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cached: GatewayConfig | None = None
        self._mtime: float | None = None

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> GatewayConfig:
        try:
            mtime = self._path.stat().st_mtime
        except OSError as exc:
            # A vanished file must not take a running gateway down if we have a
            # last-good config; only fail hard when nothing was ever loaded.
            if self._cached is not None:
                return self._cached
            raise FileNotFoundError(f"models.yaml not found at {self._path}") from exc
        if self._cached is None or mtime != self._mtime:
            try:
                parsed = parse_config(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError, yaml.YAMLError) as exc:
                if self._cached is None:
                    raise  # nothing good to fall back to — surface the error
                # A bad hot edit (invalid YAML / broken cross-reference) must not
                # 500 all LLM traffic: keep serving the last-good config and stop
                # re-parsing until the file changes again (mtime advances on fix).
                logger.warning("models_config.reload_failed", path=str(self._path), error=str(exc))
                self._mtime = mtime
                return self._cached
            self._cached = parsed
            self._mtime = mtime
        return self._cached


__all__ = [
    "Budgets",
    "ConfigLoader",
    "GatewayConfig",
    "ModelAlias",
    "Price",
    "ProviderConfig",
    "Routing",
    "parse_config",
]
