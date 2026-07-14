"""Runtime tool-grant enforcement (Vol 2 §2.1.5, TITAN-AGT-004/024).

An agent's ``tool_grants`` allow-list is enforced at the RUNTIME layer, not just
in the prompt (TITAN-AGT-004): a compromised or prompt-injected model that asks
for a tool it was never granted must be stopped by code.

Two mechanisms:

1. **Ungranted tools are invisible.** :func:`granted_tool_defs` sends the model
   ONLY the schemas of granted tools that exist in the registry — an ungranted
   tool's schema is never in the request, so the model has no handle to call it.
2. **Ungranted calls are rejected + logged.** Even so, a model may hallucinate a
   tool name. :func:`is_granted` gates every model-requested call; a call outside
   the allow-list is refused before it ever reaches the Tool Bus and recorded as
   a :class:`SecurityEvent` on the security-event log (R2 / Vol 8).

Granted calls go through the Phase-18 Tool Bus, which schema-validates, authorizes
(role allowlist + scopes + risk gate), and audits every execution (result hash)
per TITAN-AGT-024 — the runtime does not duplicate that; it feeds the bus the
caller identity.
"""

import uuid
from dataclasses import dataclass

import structlog

from titan.llm.service import ToolDef
from titan.tools.service import ToolRegistry

logger = structlog.get_logger("titan.runtime.grants")

# agent_id → the Vol 8 §4.3 tool-allowlist role the Tool Bus authorizes against.
# The pure-coordination roles orchestrator and critic are absent on purpose: they
# get no bus tools (fail closed) — they act through messages, not side-effecting
# tools. Every other roster role maps to its §4.3 row (pm_product/pm_project to the
# task/messaging bundle; builders and gatekeepers to theirs).
AGENT_ROLE: dict[str, str] = {
    "ceo": "ceo",
    "pm_product": "pm",
    "pm_project": "project_manager",
    "architect": "architect",
    "eng_backend": "backend",
    "eng_frontend": "frontend",
    "eng_database": "db",
    "eng_devops": "devops",
    "qa": "qa",
    "security": "security",
    "reviewer": "reviewer",
    "docs": "docs",
    "research": "research",
    "memory": "memory",
}


@dataclass(frozen=True)
class SecurityEvent:
    """A record of a runtime-blocked, ungranted tool request (TITAN-AGT-004
    acceptance). Carries the tool name and the count of argument keys — never the
    argument VALUES, which are model-controlled and may embed injected content
    (TITAN-OPS-016)."""

    agent_id: str
    project_id: uuid.UUID
    task_id: uuid.UUID | None
    tool: str
    arg_keys: int
    reason: str = "tool not in the agent's tool_grants allow-list"


def granted_tool_defs(registry: ToolRegistry, tool_grants: tuple[str, ...]) -> list[ToolDef]:
    """The tool schemas an agent may see this turn: granted ∩ registered, in the
    gateway's canonical format. A grant with no registered tool (a not-yet-built
    tool the roster already anticipates) contributes no schema — the model cannot
    call a tool that does not exist yet. Ungranted tools are never included."""
    defs: list[ToolDef] = []
    for name in tool_grants:
        spec = registry.get(name)
        if spec is not None:
            defs.append(spec.tool_def())
    return defs


#: Tool providers whose required_scopes must NEVER contribute to an agent's authority
#: (ADR-INT-04). A plugin/MCP server is third-party code: if its tool's declared scopes
#: were folded into the caller's granted set, granting an agent that tool would
#: SELF-GRANT the scopes and the Tool Bus check would be vacuous — precisely the
#: "low-privilege agent → high-privilege plugin" laundering path the threat model
#: forbids. Their scopes must instead be satisfied by the agent's CORE grants.
_NON_AUTHORITATIVE_PREFIXES = ("plugin:", "mcp:")


def granted_scopes(registry: ToolRegistry, tool_grants: tuple[str, ...]) -> frozenset[str]:
    """The RBAC scopes an agent carries this turn: the union of the required
    scopes of its granted, registered tools. Modelling authority as "exactly what
    your granted tools need" keeps the Tool Bus scope check satisfiable for
    granted calls and unsatisfiable for anything else.

    Tools contributed by a PLUGIN or MCP server are excluded from this union: a
    third party cannot mint the very authority it is about to be checked against
    (ADR-INT-04). An agent may call a plugin tool only if its core grants already
    carry that tool's required scopes."""
    scopes: set[str] = set()
    for name in tool_grants:
        spec = registry.get(name)
        if spec is None:
            continue
        if spec.provider.startswith(_NON_AUTHORITATIVE_PREFIXES):
            continue  # never self-granting
        scopes.update(spec.required_scopes)
    return frozenset(scopes)


def is_granted(tool_grants: tuple[str, ...], tool_name: str) -> bool:
    """Whether ``tool_name`` is in the agent's allow-list (the runtime gate)."""
    return tool_name in tool_grants


def log_security_event(event: SecurityEvent) -> None:
    """Emit the ungranted-tool-call security event to the structured log — the
    platform's security-event log (Vol 9 Loki). Content-light by construction."""
    logger.warning(
        "security.tool_grant_denied",
        agent_id=event.agent_id,
        project_id=str(event.project_id),
        task_id=str(event.task_id) if event.task_id else None,
        tool=event.tool,
        arg_keys=event.arg_keys,
        reason=event.reason,
    )


__all__ = [
    "AGENT_ROLE",
    "SecurityEvent",
    "granted_scopes",
    "granted_tool_defs",
    "is_granted",
    "log_security_event",
]
