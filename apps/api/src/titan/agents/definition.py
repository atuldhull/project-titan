"""The declarative ``AgentDefinition`` schema (Vol 2 §2.1, TITAN-AGT-001).

Every TITAN agent is a *role*, not a process: a versioned bundle of system
prompt, decision authority, memory scopes, tool grants, model policy, KPIs, and
failure policy — the "employment contract" for that role. Each contract lives in
``config/agents/<agent_id>.yaml`` and is validated by the Pydantic v2 model
below. Validation is strict on purpose (``extra="forbid"`` + every one of the
eight §2.1 fields required): a typo'd key or a missing field must fail loudly at
load time, so a malformed roster refuses to boot rather than running an agent
with silently-dropped authority.

The models here own only *shape*. Cross-referential roster checks (supervisors
resolve, escalation targets exist, the core roster is complete) live in
:mod:`titan.agents.loader`, which needs the whole roster to check them.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The six memory scopes an agent may be granted read/write over (Vol 5 / §2.1.4).
MemoryScope = Literal[
    "short_term",
    "project",
    "organization",
    "code_patterns",
    "architecture",
    "lessons_learned",
]

# Model tier the agent's turns route to (§8 model-policy.yaml). The concrete
# provider/model behind each tier is gateway configuration, not agent config.
ModelTier = Literal["reasoning", "standard", "fast"]

# Agent tier in the hierarchy (§3.1): 0–3, or ``cross`` for the cross-cutting
# gatekeeper/service roles that serve every tier.
AgentTier = Literal[0, 1, 2, 3, "cross"]

# Failure-recovery posture relative to the §2.2 ladder. ``fail_closed`` gates
# (Security §4.11, Reviewer §4.12) block on outage; ``fail_open`` gates (Docs
# §4.13, Critic §4.16) never block the pipeline; ``default`` follows the ladder.
FailMode = Literal["default", "fail_open", "fail_closed"]

# Escalation targets that are not agent_ids. ``human`` is the human client;
# ``conflict_resolution`` is the §7.2 arbitration / ConflictResolution thread;
# ``owning_agent`` / ``requester`` are dynamic (resolved per interaction). The
# loader validates every other target against the loaded roster.
SPECIAL_ESCALATION_TARGETS: frozenset[str] = frozenset(
    {"human", "conflict_resolution", "owning_agent", "requester"}
)

# The 16 agent_ids that MUST be present in the default roster (§3.1 /
# TITAN-AGT-008). A roster missing any of these refuses to boot; additional
# agents beyond these (TITAN-AGT-006 extensibility) are allowed.
CORE_ROSTER: frozenset[str] = frozenset(
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

#: The agents-table role vocabulary (``titan.agents.service.AgentRole``, the §6
#: free-text roles the REST API constrains) → the CORE_ROSTER runtime id ``run_turn``
#: expects (Phase 56). ``orchestrator`` and ``critic`` are internal runtime roles
#: with no agents-table equivalent, so they are absent here.
ROLE_TO_ROSTER: dict[str, str] = {
    "ceo": "ceo",
    "pm": "pm_product",
    "project_manager": "pm_project",
    "architect": "architect",
    "backend": "eng_backend",
    "frontend": "eng_frontend",
    "db": "eng_database",
    "devops": "eng_devops",
    "qa": "qa",
    "security": "security",
    "reviewer": "reviewer",
    "docs": "docs",
    "research": "research",
    "memory": "memory",
}


def roster_id_for_role(role: str | None) -> str | None:
    """The CORE_ROSTER runtime id for an agents-table role, or None if unmapped."""
    return ROLE_TO_ROSTER.get(role) if role else None


class Escalation(BaseModel):
    """One ``must_escalate`` decision class and where it escalates to (§2.1.3).

    ``to`` is one or more targets: agent_ids, or one of
    :data:`SPECIAL_ESCALATION_TARGETS`. ``co_sign=True`` marks a decision that
    requires *both* named parties to approve (e.g. Backend's "new dependencies →
    Architect + Security"), which the runtime enforces per TITAN-AGT-011.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: str = Field(min_length=1)
    to: tuple[str, ...] = Field(min_length=1)
    co_sign: bool = False


class Authority(BaseModel):
    """What the agent decides alone vs. must escalate (§2.1.3).

    ``veto`` marks a hard blocking power: only the Security agent (§4.11 / §7.4)
    holds it in the default roster, and the runtime enforces it (TITAN-AGT-011).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decides_alone: tuple[str, ...]
    must_escalate: tuple[Escalation, ...]
    veto: bool = False


class MemoryScopes(BaseModel):
    """Read/write grants over the Volume 5 memory scopes (§2.1.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    read: tuple[MemoryScope, ...]
    write: tuple[MemoryScope, ...]


class ModelPolicy(BaseModel):
    """Which model tier, sampling, and output bounds the agent's turns use
    (§2.1.6). ``structured_output_schema`` is a schema *reference* (a name);
    the actual schema is composed at call time in Phase 28."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: ModelTier
    temperature: float = Field(ge=0.0, le=2.0)
    max_output_tokens: int = Field(gt=0)
    structured_output_schema: str | None = None


class FailurePolicy(BaseModel):
    """Overrides to the default §2.2 recovery ladder (§2.1.8).

    ``mode`` captures the fail-open/closed posture the runtime enforces;
    ``escalates_to_human`` (CEO §4.1: no supervisor agent) and
    ``watchdog_restart`` (Orchestrator §4.2 / TITAN-AGT-009: non-LLM watchdog)
    are the other mechanically-checkable deltas. ``skip_retries_on`` lists
    decision classes that bypass the retry rungs and escalate immediately (DB
    §4.8: destructive migrations). ``notes`` records the remaining prose delta
    verbatim for prompt rendering and audit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: FailMode = "default"
    escalates_to_human: bool = False
    watchdog_restart: bool = False
    skip_retries_on: tuple[str, ...] = ()
    notes: str | None = None


class AgentDefinition(BaseModel):
    """The complete, validated contract for one agent role (§2.1).

    All eight §2.1 field-groups are required — ``extra="forbid"`` plus the lack
    of top-level defaults means a missing or misspelled field fails validation,
    satisfying TITAN-AGT-001's "a missing field fails CI".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # (1) identity
    agent_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    tier: AgentTier
    supervisor: str = Field(min_length=1)  # an agent_id, or "human" for the CEO
    # (2) prompt
    system_prompt_template: str = Field(min_length=1)
    # (3) authority
    authority: Authority
    # (4) memory
    memory_scopes: MemoryScopes
    # (5) tools — allow-list of tool-registry IDs; ungranted tools are invisible
    tool_grants: tuple[str, ...]
    # (6) model
    model_policy: ModelPolicy
    # (7) KPIs — metric names emitted to Prometheus via OpenTelemetry
    kpis: tuple[str, ...]
    # (8) failure policy
    failure_policy: FailurePolicy


__all__ = [
    "CORE_ROSTER",
    "ROLE_TO_ROSTER",
    "SPECIAL_ESCALATION_TARGETS",
    "AgentDefinition",
    "AgentTier",
    "Authority",
    "Escalation",
    "FailMode",
    "FailurePolicy",
    "MemoryScope",
    "MemoryScopes",
    "ModelPolicy",
    "ModelTier",
]
