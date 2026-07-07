"""Per-agent authority contract — the mechanically-checkable slice of §4 (TITAN-AGT-011).

Each agent's ``decides_alone`` / ``must_escalate`` lists are prose, but three parts
are machine-enforceable and the runtime enforces them: the **veto** power (only
Security holds it, §7.4), **co-signs** (a decision class that needs two named
approvers — destructive migrations need PM + DevOps §4.8, secret-handling needs
Security §4.9, security-relevant design needs Security §4.4), and **fail-open vs.
fail-closed** posture (Security/Reviewer closed, Docs/Critic open). This module
projects an :class:`AgentDefinition` onto exactly those facts so the §4 contract
tests assert against data, not prose — the drift guard R5 warns about.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from titan.agents.service import AgentDefinition

FailMode = Literal["default", "fail_open", "fail_closed"]


@dataclass(frozen=True)
class CoSignRequirement:
    """A decision class that requires *all* named parties to approve (§2.1.3)."""

    decision: str
    cosigners: tuple[str, ...]


@dataclass(frozen=True)
class AuthorityContract:
    """The mechanically-checkable authority facts for one agent (TITAN-AGT-011)."""

    agent_id: str
    has_veto: bool
    mode: FailMode
    cosign_requirements: tuple[CoSignRequirement, ...]

    @property
    def fail_open(self) -> bool:
        return self.mode == "fail_open"

    @property
    def fail_closed(self) -> bool:
        return self.mode == "fail_closed"

    def requires_cosign(self, *cosigners: str) -> bool:
        """Whether the agent has a co-sign rule naming EXACTLY ``cosigners`` (order
        independent) — e.g. ``requires_cosign("pm_project", "eng_devops")``."""
        wanted = frozenset(cosigners)
        return any(frozenset(r.cosigners) == wanted for r in self.cosign_requirements)


def contract_for(definition: AgentDefinition) -> AuthorityContract:
    """Project one agent's :class:`AgentDefinition` onto its authority contract."""
    cosigns = tuple(
        CoSignRequirement(decision=e.decision, cosigners=tuple(e.to))
        for e in definition.authority.must_escalate
        if e.co_sign
    )
    return AuthorityContract(
        agent_id=definition.agent_id,
        has_veto=definition.authority.veto,
        mode=definition.failure_policy.mode,
        cosign_requirements=cosigns,
    )


def vetoing_agents(definitions: Sequence[AgentDefinition]) -> frozenset[str]:
    """The set of roles that hold a blocking veto — exactly ``{"security"}`` in the
    default roster (§7.4). The veto gate consults this so the power is data-driven,
    not a hard-coded ``== "security"`` check."""
    return frozenset(d.agent_id for d in definitions if d.authority.veto)


__all__ = [
    "AuthorityContract",
    "CoSignRequirement",
    "FailMode",
    "contract_for",
    "vetoing_agents",
]
