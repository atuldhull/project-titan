"""Public service interface of the `tools` module (TITAN-BCK-004).

Responsibility: Tool Registry + Tool Bus — the single chokepoint for agent
side-effects (Vol 7 §4, TITAN-INT-001). Owns: tool_audit.

Only this interface may be imported by other modules; the bus internals,
allowlist matrix, and audit repository stay private to the package. Later
phases register their tools (fs.*, git.*, …) through :class:`ToolSpec` +
:meth:`ToolRegistry.register` and swap the Phase-19 approval gate in via
``build_bus(approval_gate=...)``.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.tools import repository as _repo
from titan.tools.builtin import register_builtin
from titan.tools.bus import ApprovalGate, ToolBus, ToolCaller, deny_all_approvals
from titan.tools.envelopes import Artifact, ToolCallEnvelope, ToolResultEnvelope, new_call
from titan.tools.errors import TOOL_ERROR_CODES, ToolError, ToolErrorCode
from titan.tools.redaction import Redactor, default_redactor
from titan.tools.registry import (
    ExecutionOutcome,
    ParamConstraint,
    ProjectToolPolicy,
    ToolExecutionContext,
    ToolExecutor,
    ToolRegistry,
    ToolSpec,
)


def build_registry() -> ToolRegistry:
    """The boot-time registry rebuild (§4.1): builtin provider modules today;
    plugin/MCP providers join the same rebuild in their phases."""
    registry = ToolRegistry()
    register_builtin(registry)
    return registry


def build_bus(
    engine: AsyncEngine,
    *,
    registry: ToolRegistry | None = None,
    redactor: Redactor = default_redactor,
    approval_gate: ApprovalGate = deny_all_approvals,
) -> ToolBus:
    return ToolBus(
        registry if registry is not None else build_registry(),
        engine,
        redactor=redactor,
        approval_gate=approval_gate,
    )


async def verify_audit_chain(conn: AsyncConnection, project_id: uuid.UUID) -> int | None:
    """TITAN-INT-024 verification job entry point: the seq of the first
    tampered row in the project's chain, or None when intact."""
    return await _repo.verify_chain(conn, project_id)


__all__ = [
    "TOOL_ERROR_CODES",
    "ApprovalGate",
    "Artifact",
    "ExecutionOutcome",
    "ParamConstraint",
    "ProjectToolPolicy",
    "Redactor",
    "ToolBus",
    "ToolCallEnvelope",
    "ToolCaller",
    "ToolError",
    "ToolErrorCode",
    "ToolExecutionContext",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResultEnvelope",
    "ToolSpec",
    "build_bus",
    "build_registry",
    "default_redactor",
    "deny_all_approvals",
    "new_call",
    "verify_audit_chain",
]
