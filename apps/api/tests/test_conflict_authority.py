"""Per-agent authority contract tests over all 16 roster roles (TITAN-AGT-011).

These are the mechanically-checkable rules §4 says the runtime enforces: the
Security veto, the co-signs (destructive migrations need PM + DevOps §4.8,
secret-handling needs Security §4.9, security-relevant design needs Security §4.4),
and fail-open/closed posture (Security/Reviewer closed, Docs/Critic open). Loads
the real YAML roster — no DB — so a drift between prose and the encoded contract
fails here (guards R5)."""

from __future__ import annotations

import pytest

from titan.agents.service import AgentRegistry, load_roster
from titan.conflict.service import all_authority_contracts, authority_contract, vetoing_roles
from titan.settings import Settings

# The 16-role core roster (§3.1 / TITAN-AGT-008).
_CORE = frozenset(
    {
        "ceo",
        "orchestrator",
        "pm_product",
        "architect",
        "pm_project",
        "eng_backend",
        "eng_frontend",
        "eng_database",
        "eng_devops",
        "qa",
        "security",
        "reviewer",
        "docs",
        "research",
        "memory",
        "critic",
    }
)


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return load_roster(Settings())


def _contract(registry: AgentRegistry, agent_id: str):  # type: ignore[no-untyped-def]
    definition = registry.get(agent_id)
    assert definition is not None, f"roster missing {agent_id}"
    return authority_contract(definition)


def test_contract_covers_all_sixteen_agents(registry: AgentRegistry) -> None:
    contracts = {c.agent_id: c for c in all_authority_contracts(registry)}
    assert set(contracts) >= _CORE, f"missing: {_CORE - set(contracts)}"


def test_only_security_holds_the_veto(registry: AgentRegistry) -> None:
    # §7.4: the blocking veto is Security's alone in the default roster.
    assert vetoing_roles(registry) == frozenset({"security"})
    assert _contract(registry, "security").has_veto
    for agent_id in _CORE - {"security"}:
        assert not _contract(registry, agent_id).has_veto, f"{agent_id} must not veto"


def test_fail_closed_and_fail_open_posture(registry: AgentRegistry) -> None:
    # §4.11/§4.12: Security and Reviewer are fail-closed (block on outage).
    assert _contract(registry, "security").fail_closed
    assert _contract(registry, "reviewer").fail_closed
    # §4.13/§4.16: Docs and Critic are fail-open (never block the pipeline).
    assert _contract(registry, "docs").fail_open
    assert _contract(registry, "critic").fail_open
    # Everyone else follows the default ladder — neither open nor closed.
    for agent_id in _CORE - {"security", "reviewer", "docs", "critic"}:
        c = _contract(registry, agent_id)
        assert not c.fail_open and not c.fail_closed, f"{agent_id} should be default mode"


def test_cosign_requirements_are_encoded(registry: AgentRegistry) -> None:
    # §4.8: a destructive migration needs BOTH the PM and DevOps to co-sign.
    assert _contract(registry, "eng_database").requires_cosign("pm_project", "eng_devops")
    # §4.9: secret-handling design needs Security to co-sign.
    assert _contract(registry, "eng_devops").requires_cosign("security")
    # §4.4: security-relevant design is co-signed by Security.
    assert _contract(registry, "architect").requires_cosign("security")


def test_cosign_is_order_independent_and_exact(registry: AgentRegistry) -> None:
    db = _contract(registry, "eng_database")
    # Order does not matter…
    assert db.requires_cosign("eng_devops", "pm_project")
    # …but the set must match exactly — a single missing co-signer is not the rule.
    assert not db.requires_cosign("pm_project")
    assert not db.requires_cosign("pm_project", "eng_devops", "security")


def test_agents_without_cosigns_have_none(registry: AgentRegistry) -> None:
    # The CEO decides releases alone; it has no co-sign obligations.
    assert _contract(registry, "ceo").cosign_requirements == ()
    assert not _contract(registry, "qa").requires_cosign("security")
