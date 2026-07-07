"""Agent model-policy resolver (Vol 2 §8, TITAN-AGT-021/026).

Config-only model switching: an agent maps to a tier, a tier to a provider/model;
changing either — or activating the local Ollama profile, or the Critic pin — is a
YAML edit that the resolver reflects with zero code change. Pure unit tests.
"""

from pathlib import Path

import pytest

from titan.agents.model_policy import (
    ModelPolicyError,
    load_model_policy,
    parse_model_policy,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_PATH = REPO_ROOT / "config" / "model-policy.yaml"

_YAML = """
tiers:
  reasoning: { provider: anthropic, model: opus, temperature: 0.2 }
  standard:  { provider: anthropic, model: sonnet, temperature: 0.3 }
  fast:      { provider: openai, model: mini, temperature: 0.2 }
profiles:
  local-free:
    reasoning: { provider: ollama, model: qwen-32b }
    standard:  { provider: ollama, model: qwen-14b }
    fast:      { provider: ollama, model: llama-3b }
agents:
  ceo: reasoning
  eng_backend: standard
  pm_project: fast
  critic: reasoning
"""


def test_resolves_agent_to_tier_provider_model() -> None:
    p = parse_model_policy(_YAML)
    ceo = p.resolve("ceo")
    assert (ceo.provider, ceo.model, ceo.tier, ceo.temperature) == (
        "anthropic",
        "opus",
        "reasoning",
        0.2,
    )
    be = p.resolve("eng_backend")
    assert (be.provider, be.model, be.temperature) == ("anthropic", "sonnet", 0.3)


def test_profile_overrides_provider_model_config_only() -> None:
    """TITAN-AGT-021: the local-free profile flips every agent to Ollama with no
    code change — the resolver simply reads the profile."""
    p = parse_model_policy(_YAML)
    ceo = p.resolve("ceo", profile="local-free")
    assert (ceo.provider, ceo.model) == ("ollama", "qwen-32b")
    assert ceo.temperature == 0.2  # profile omits temperature -> inherit base tier's


def test_changing_an_agents_tier_resolves_a_new_model() -> None:
    """DoD: change an agent's tier in the policy and a NEW provider/model resolves,
    zero code change."""
    before = parse_model_policy(_YAML).resolve("eng_backend")
    assert before.model == "sonnet"
    after = parse_model_policy(_YAML.replace("eng_backend: standard", "eng_backend: fast")).resolve(
        "eng_backend"
    )
    assert (after.tier, after.provider, after.model) == ("fast", "openai", "mini")


def test_critic_pin_routes_to_second_provider() -> None:
    """TITAN-AGT-026: an optional pin routes the Critic to a different provider."""
    p = parse_model_policy(_YAML)
    assert p.resolve("critic").provider == "anthropic"  # off by default
    pinned = parse_model_policy(
        _YAML + "critic_pin: { provider: google, model: gemini, temperature: 0.1 }\n"
    )
    critic = pinned.resolve("critic")
    assert (critic.provider, critic.model, critic.temperature) == ("google", "gemini", 0.1)
    # The pin is Critic-only; other agents are unaffected.
    assert pinned.resolve("ceo").provider == "anthropic"


def test_unknown_agent_raises() -> None:
    with pytest.raises(ModelPolicyError):
        parse_model_policy(_YAML).resolve("nope")


def test_agent_mapped_to_unknown_tier_rejected() -> None:
    with pytest.raises(ModelPolicyError):
        parse_model_policy(_YAML.replace("ceo: reasoning", "ceo: genius"))


def test_extra_field_rejected() -> None:
    with pytest.raises(ValueError):
        parse_model_policy(_YAML + "surprise: 1\n")


# --- the shipped config/model-policy.yaml (§8) ------------------------------

_EXPECTED_TIER = {
    "ceo": "reasoning",
    "architect": "reasoning",
    "critic": "reasoning",
    "security": "reasoning",
    "eng_backend": "standard",
    "eng_frontend": "standard",
    "eng_database": "standard",
    "eng_devops": "standard",
    "reviewer": "standard",
    "qa": "standard",
    "pm_product": "standard",
    "orchestrator": "standard",
    "pm_project": "fast",
    "docs": "fast",
    "research": "fast",
    "memory": "fast",
}


def test_shipped_policy_matches_section8_agent_map() -> None:
    p = load_model_policy(POLICY_PATH)
    assert p.agents == _EXPECTED_TIER
    assert set(p.tiers) == {"reasoning", "standard", "fast"}


def test_shipped_local_free_profile_is_ollama() -> None:
    p = load_model_policy(POLICY_PATH)
    for agent_id in _EXPECTED_TIER:
        sel = p.resolve(agent_id, profile="local-free")
        assert sel.provider == "ollama", agent_id
    # The §8 local-free models.
    assert p.resolve("architect", profile="local-free").model == "qwen2.5-coder:32b"
    assert p.resolve("pm_project", profile="local-free").model == "llama3.2:3b"
