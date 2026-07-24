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
    """Budget caps (§8.4, TITAN-SEC-082). The Phase-18 cumulative caps
    (``per_project_usd``, ``per_task_usd``) plus the Phase-71 tier extension:

    * ``per_project_day_usd`` — a DAILY window per project (resets at UTC midnight);
    * ``per_org_month_usd`` — the global monthly ceiling. TITAN is single-org by
      construction (no ``organizations`` table — see ``identity.accounts``), so
      Vol 9 §10's "global monthly ceiling → per-organization budget" collapse to
      ONE deployment-wide monthly cap, keyed on a singleton scope;
    * ``local_seconds_per_task`` — local models are $0, so a dollar cap never
      bounds a runaway LOCAL loop; this time-caps cumulative local-model wall
      time per task instead (TITAN-SEC-082 "local models time-capped").

    Windowed caps bake the period into their Redis key + hold scope, so a new day
    or month starts fresh with no bookkeeping and a stale hold is simply never
    consulted again (the resume is automatic at rollover)."""

    model_config = ConfigDict(extra="forbid")
    per_project_usd: Decimal | None = None
    per_task_usd: Decimal | None = None
    per_project_day_usd: Decimal | None = None
    per_org_month_usd: Decimal | None = None
    local_seconds_per_task: int | None = None
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


#: Keys the overlay may override, and how. ``providers`` and ``model_aliases``
#: merge per-entry (an overlay can add one provider without restating the rest);
#: ``routing`` and ``budgets`` merge per-field. Anything else is replaced whole.
_PER_ENTRY_KEYS = ("providers", "model_aliases")
_PER_FIELD_KEYS = ("routing", "budgets")

#: Keys the overlay may carry that are NOT part of models.yaml. ``GatewayConfig``
#: is ``extra="forbid"``, so anything outside its schema has to be dropped before
#: validation or the whole overlay is rejected. The overlay is also the Settings
#: surface's own store — ``agent_profile`` (the model-policy profile) lives there
#: because it is the same operator decision and must be read on the same mtime
#: check — so those keys are stripped here rather than being forbidden.
_NON_GATEWAY_KEYS = frozenset({"agent_profile"})


def resolve_config_path(configured: str | Path) -> Path:
    """Resolve a relative config path against the nearest enclosing repo root.

    ``models_config_path`` defaults to the relative ``config/models.yaml``, so
    the gateway only ever loaded when the process happened to be started from
    the repository root. Anything with a different working directory — pytest
    run from ``apps/api``, a CLI invoked from a subdirectory — got
    ``FileNotFoundError`` from the very first ``get()``.

    Deliberately duplicates ``titan.agents.loader.resolve_config_file`` (ten
    lines) rather than importing it: ``llm`` importing ``agents`` would couple
    two domain modules across a seam the boundary check exists to protect, and
    the cure would be worse than the duplication.
    """
    path = Path(configured)
    if path.is_absolute():
        return path
    cwd = Path.cwd()
    for base in (cwd, *cwd.parents):
        if (base / path).is_file():
            return base / path
        if (base / ".git").exists():  # repo boundary — never search above it
            break
    return cwd / path


def overlay_path_for(base: Path) -> Path:
    """``config/models.yaml`` → ``config/models.overrides.yaml``.

    Derived rather than configured, so all three ``ConfigLoader`` call sites
    pick the overlay up without threading a new setting through them, and so
    the offline profile (``models.local.yaml``) gets its own separate overlay
    instead of silently inheriting the cloud profile's overrides.
    """
    return base.with_name(f"{base.stem}.overrides{base.suffix}")


def merge_overlay(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Overlay over base, one level deep on the keys where that is meaningful.

    Deliberately NOT a general recursive merge: a model alias is a whole record
    (provider + model + price + context window), so half-overriding one would
    produce an alias whose price belongs to a different model. Entries are
    replaced whole; only their containers merge.
    """
    merged = dict(base)
    for key, value in overlay.items():
        if key in _NON_GATEWAY_KEYS:
            continue
        if key in (*_PER_ENTRY_KEYS, *_PER_FIELD_KEYS) and isinstance(value, dict):
            existing = merged.get(key)
            merged[key] = {**existing, **value} if isinstance(existing, dict) else dict(value)
        else:
            merged[key] = value
    return merged


class ConfigLoader:
    """Loads and caches ``models.yaml``, reloading on mtime change (§8.2 hot
    reload). ``get()`` is intentionally synchronous — a stat + small read is
    sub-millisecond and must not be awaited from adapter hot paths.

    An optional sibling ``models.overrides.yaml`` is merged over the base when
    present. That file is the ONLY thing the Settings API writes: the
    hand-maintained ``models.yaml`` keeps its comments and stays the operator's,
    while a UI change lands in a small generated file that can simply be deleted
    to revert. Because the merge happens inside the same mtime-checked read, a
    Settings write is picked up by every process on its next request with no
    restart and no DB round-trip on the hot path — which is what makes
    TITAN-FRT-036 ("switching the default provider takes effect without
    redeploying") true across the API *and* the worker, not just in whichever
    process happened to serve the write.
    """

    def __init__(self, path: Path, overlay_path: Path | None = None) -> None:
        self._path = resolve_config_path(path)
        self._overlay_path = (
            overlay_path if overlay_path is not None else overlay_path_for(self._path)
        )
        self._cached: GatewayConfig | None = None
        self._stamp: tuple[float, float | None] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def overlay_path(self) -> Path:
        return self._overlay_path

    def invalidate(self) -> None:
        """Drop the cache so the next ``get()`` re-reads from disk.

        The mtime check alone is not enough for read-your-own-write: filesystem
        timestamp granularity is coarse (a whole second on some filesystems), so
        a write followed immediately by a read can land inside the same tick and
        be served the pre-write config. The writer calls this; other processes
        still converge via mtime, which is all they need.
        """
        self._cached = None
        self._stamp = None

    @staticmethod
    def _stat(path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def get(self) -> GatewayConfig:
        base_mtime = self._stat(self._path)
        if base_mtime is None:
            # A vanished file must not take a running gateway down if we have a
            # last-good config; only fail hard when nothing was ever loaded.
            if self._cached is not None:
                return self._cached
            raise FileNotFoundError(f"models.yaml not found at {self._path}")
        stamp = (base_mtime, self._stat(self._overlay_path))
        if self._cached is None or stamp != self._stamp:
            try:
                parsed = self._read_merged()
            except (OSError, ValueError, yaml.YAMLError) as exc:
                if self._cached is None:
                    raise  # nothing good to fall back to — surface the error
                # A bad hot edit (invalid YAML / broken cross-reference) must not
                # 500 all LLM traffic: keep serving the last-good config and stop
                # re-parsing until a file changes again (mtime advances on fix).
                logger.warning("models_config.reload_failed", path=str(self._path), error=str(exc))
                self._stamp = stamp
                return self._cached
            self._cached = parsed
            self._stamp = stamp
        return self._cached

    def _read_merged(self) -> GatewayConfig:
        data = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("models.yaml must be a mapping at the top level")
        try:
            raw_overlay = self._overlay_path.read_text(encoding="utf-8")
        except OSError:
            merged = data
        else:
            overlay = yaml.safe_load(raw_overlay)
            if overlay is None:  # an empty overlay file means "no overrides"
                overlay = {}
            if not isinstance(overlay, dict):
                raise ValueError(f"{self._overlay_path.name} must be a mapping at the top level")
            merged = merge_overlay(data, overlay)
        # Validation runs on the MERGED document, so an overlay that points
        # routing.default at an alias nobody defines is rejected as a unit and
        # the last-good config keeps serving. An overlay cannot break the
        # gateway merely by being well-formed on its own.
        return GatewayConfig.model_validate(_resolve_env(merged))


__all__ = [
    "Budgets",
    "ConfigLoader",
    "GatewayConfig",
    "ModelAlias",
    "Price",
    "ProviderConfig",
    "Routing",
    "merge_overlay",
    "resolve_config_path",
    "overlay_path_for",
    "parse_config",
]
