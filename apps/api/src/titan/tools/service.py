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
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.tools import repository as _repo
from titan.tools.approvals import (
    ApprovalStatus,
    DbApprovalGate,
    ToolApprovalOut,
    list_approvals,
    resolve_approval,
)
from titan.tools.builtin import register_builtin
from titan.tools.bus import (
    ApprovalDecision,
    ApprovalGate,
    RateLimiter,
    ToolBus,
    ToolCaller,
    WorkspaceProvider,
    deny_all_approvals,
)
from titan.tools.constraints import argv_only, url_allowlist, workspace_path
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
from titan.tools.sandbox.controller import ExecResult, Sandbox, SandboxController
from titan.tools.sandbox.policy import ResourceLimits, SandboxPolicy
from titan.tools.sandbox.session import SandboxSession, SandboxSessionManager
from titan.tools.sandbox.workspace import Workspace, WorkspaceManager
from titan.tools.streaming import ToolOutputStream, channel_for


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
    approval_gate: ApprovalGate | None = None,
    rate_limiter: RateLimiter | None = None,
    workspace_provider: WorkspaceProvider | None = None,
    sandbox_manager: SandboxSessionManager | None = None,
    redis: Redis | None = None,
) -> ToolBus:
    """The production bus. The admin-risk gate defaults to the DB-backed
    approval-record flow (TITAN-INT-022); pass ``approval_gate`` to override
    (e.g. ``deny_all_approvals`` in a closed test). ``workspace_provider`` gives
    fs.* tools their jailed /workspace (Phase 20); ``sandbox_manager`` + ``redis``
    give terminal/git/code tools their per-task sandbox + live output stream
    (Phase 21)."""
    gate = approval_gate if approval_gate is not None else DbApprovalGate(engine, redactor=redactor)
    return ToolBus(
        registry if registry is not None else build_registry(),
        engine,
        redactor=redactor,
        approval_gate=gate,
        rate_limiter=rate_limiter,
        workspace_provider=workspace_provider,
        sandbox_manager=sandbox_manager,
        redis=redis,
    )


def build_workspace_manager(root: str) -> WorkspaceManager:
    """The per-task workspace manager (a :class:`WorkspaceProvider`) rooted at
    ``root`` on the host (TITAN-SEC-040)."""
    return WorkspaceManager(Path(root))


def build_sandbox_controller(image: str, *, docker_host: str = "") -> SandboxController:
    """The Sandbox Controller for hardened per-task code execution (Vol 8 §5).
    Reachable only when a Docker daemon is available; :meth:`SandboxController.ping`
    tells callers to degrade gracefully when it is not."""
    return SandboxController(SandboxPolicy(image=image), docker_host=docker_host)


def build_sandbox_manager(controller: SandboxController) -> SandboxSessionManager:
    """The per-task sandbox-session pool the terminal/git/code tools run inside."""
    return SandboxSessionManager(controller)


async def verify_audit_chain(conn: AsyncConnection, project_id: uuid.UUID) -> int | None:
    """TITAN-INT-024 verification job entry point: the seq of the first
    tampered row in the project's chain, or None when intact."""
    return await _repo.verify_chain(conn, project_id)


__all__ = [
    "TOOL_ERROR_CODES",
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalStatus",
    "Artifact",
    "DbApprovalGate",
    "ExecResult",
    "ExecutionOutcome",
    "ParamConstraint",
    "ProjectToolPolicy",
    "RateLimiter",
    "Redactor",
    "ResourceLimits",
    "Sandbox",
    "SandboxController",
    "SandboxPolicy",
    "SandboxSession",
    "SandboxSessionManager",
    "ToolApprovalOut",
    "ToolOutputStream",
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
    "Workspace",
    "WorkspaceManager",
    "WorkspaceProvider",
    "argv_only",
    "build_bus",
    "build_registry",
    "build_sandbox_controller",
    "build_sandbox_manager",
    "build_workspace_manager",
    "channel_for",
    "default_redactor",
    "deny_all_approvals",
    "list_approvals",
    "new_call",
    "resolve_approval",
    "url_allowlist",
    "verify_audit_chain",
    "workspace_path",
]
