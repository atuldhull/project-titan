"""Agent model-policy resolver (Vol 2 §8, TITAN-AGT-021/026).

``config/model-policy.yaml`` maps each agent to a tier, and each tier to a
provider+model+temperature. This module loads + validates that policy and
resolves ``agent_id -> ModelSelection`` per turn, so switching any agent's
provider/model — or flipping the whole roster onto the local Ollama profile — is a
config edit with ZERO code change (TITAN-AGT-021). The Critic may be pinned to a
different provider than the artifact author (TITAN-AGT-026), off by default.

The resolver produces the concrete provider/model the runtime hands to the
Phase-15 gateway; the gateway keeps owning connection details, pricing, and
fallbacks (config/models.yaml).
"""

from functools import lru_cache
from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from titan.agents.loader import resolve_config_file

logger = structlog.get_logger("titan.agents.model_policy")

# ``model``/``model_policy`` collide with Pydantic's protected namespace.
_ALLOW_MODEL = ConfigDict(protected_namespaces=(), extra="forbid")


class ModelPolicyError(ValueError):
    """The model policy is malformed or references an unknown tier."""


class TierSpec(BaseModel):
    """A concrete provider+model (+ optional sampling) for one tier. In a profile
    the temperature may be omitted, inheriting the base tier's."""

    model_config = _ALLOW_MODEL
    provider: str
    model: str
    temperature: float | None = None


class ModelSelection(BaseModel):
    """The resolved model for one agent turn."""

    model_config = _ALLOW_MODEL
    agent_id: str
    tier: str
    provider: str
    model: str
    temperature: float


class ModelPolicyConfig(BaseModel):
    """The whole ``model-policy.yaml`` (§8), cross-referentially valid."""

    model_config = _ALLOW_MODEL
    tiers: dict[str, TierSpec]
    profiles: dict[str, dict[str, TierSpec]] = {}
    agents: dict[str, str]
    # TITAN-AGT-026: pin the Critic to a second provider (off by default).
    critic_pin: TierSpec | None = None

    @model_validator(mode="after")
    def _validate_references(self) -> "ModelPolicyConfig":
        tier_names = set(self.tiers)
        for agent_id, tier in self.agents.items():
            if tier not in tier_names:
                raise ModelPolicyError(
                    f"agent '{agent_id}' maps to unknown tier '{tier}' (have {sorted(tier_names)})"
                )
        for profile, overrides in self.profiles.items():
            for tier in overrides:
                if tier not in tier_names:
                    raise ModelPolicyError(f"profile '{profile}' overrides unknown tier '{tier}'")
        return self

    def resolve(self, agent_id: str, *, profile: str | None = None) -> ModelSelection:
        """Resolve ``agent_id`` to a concrete provider/model, honouring the active
        profile and the optional Critic pin."""
        tier = self.agents.get(agent_id)
        if tier is None:
            raise ModelPolicyError(f"no model policy for agent '{agent_id}'")
        base = self.tiers[tier]
        spec = base
        if profile is not None:
            override = self.profiles.get(profile, {}).get(tier)
            if override is not None:
                # A profile may omit temperature — inherit the base tier's.
                spec = TierSpec(
                    provider=override.provider,
                    model=override.model,
                    temperature=override.temperature
                    if override.temperature is not None
                    else base.temperature,
                )
        # The Critic pin wins outright when enabled (multi-model critique).
        if agent_id == "critic" and self.critic_pin is not None:
            spec = self.critic_pin
        return ModelSelection(
            agent_id=agent_id,
            tier=tier,
            provider=spec.provider,
            model=spec.model,
            temperature=spec.temperature if spec.temperature is not None else base.temperature,
        )


def parse_model_policy(raw: str) -> ModelPolicyConfig:
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ModelPolicyError("model-policy.yaml must be a mapping at the top level")
    try:
        return ModelPolicyConfig.model_validate(data)
    except ValidationError as exc:
        # Surface every config error as one ModelPolicyError (uniform API), not a
        # leaked Pydantic ValidationError for cross-reference failures.
        raise ModelPolicyError(f"invalid model-policy.yaml:\n{exc}") from exc


def load_model_policy(path: str | Path) -> ModelPolicyConfig:
    resolved = resolve_config_file(path)
    if not resolved.is_file():
        raise ModelPolicyError(f"model-policy.yaml not found at {resolved}")
    policy = parse_model_policy(resolved.read_text(encoding="utf-8"))
    logger.info("model_policy.loaded", agents=len(policy.agents), path=str(resolved))
    return policy


@lru_cache(maxsize=4)
def load_model_policy_cached(path: str) -> ModelPolicyConfig:
    """Process-wide cached load, keyed on the resolved absolute path."""
    return load_model_policy(str(resolve_config_file(path)))


__all__ = [
    "ModelPolicyConfig",
    "ModelPolicyError",
    "ModelSelection",
    "TierSpec",
    "load_model_policy",
    "load_model_policy_cached",
    "parse_model_policy",
]
