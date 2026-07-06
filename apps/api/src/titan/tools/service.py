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

import httpx
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
from titan.tools.browser.controller import BrowserController, BrowserServer
from titan.tools.browser.session import BrowserSession, BrowserSessionManager
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
from titan.tools.dind.controller import APP_PORTS, DindController, DindSidecar
from titan.tools.dind.session import DindSession, DindSessionManager
from titan.tools.egress.hosts import (
    EgressHost,
    add_approved_host,
    is_host_approved,
    list_approved_hosts,
    remove_approved_host,
)
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
from titan.tools.sandbox.policy import EgressConfig, ResourceLimits, SandboxPolicy
from titan.tools.sandbox.session import SandboxSession, SandboxSessionManager
from titan.tools.sandbox.workspace import Workspace, WorkspaceManager
from titan.tools.streaming import ToolOutputStream, channel_for
from titan.tools.web.session import ResearchClient, ResearchSession


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
    dind_manager: DindSessionManager | None = None,
    browser_manager: BrowserSessionManager | None = None,
    research_client: ResearchClient | None = None,
    redis: Redis | None = None,
) -> ToolBus:
    """The production bus. The admin-risk gate defaults to the DB-backed
    approval-record flow (TITAN-INT-022); pass ``approval_gate`` to override
    (e.g. ``deny_all_approvals`` in a closed test). ``workspace_provider`` gives
    fs.* tools their jailed /workspace (Phase 20); ``sandbox_manager`` + ``redis``
    give terminal/git/code tools their per-task sandbox + live output stream
    (Phase 21); ``dind_manager`` / ``browser_manager`` give docker.* / browser.*
    their per-task DinD sidecar and headless page (Phase 22); ``research_client``
    gives web.* their caching research gateway (Phase 23)."""
    gate = approval_gate if approval_gate is not None else DbApprovalGate(engine, redactor=redactor)
    return ToolBus(
        registry if registry is not None else build_registry(),
        engine,
        redactor=redactor,
        approval_gate=gate,
        rate_limiter=rate_limiter,
        workspace_provider=workspace_provider,
        sandbox_manager=sandbox_manager,
        dind_manager=dind_manager,
        browser_manager=browser_manager,
        research_client=research_client,
        redis=redis,
    )


def build_research_client(
    http: httpx.AsyncClient, *, gateway_url: str, token: str
) -> ResearchClient:
    """The web.* tools' client to the caching research fetch-gateway (Phase 23)."""
    return ResearchClient(http, gateway_url=gateway_url, token=token)


def build_workspace_manager(root: str) -> WorkspaceManager:
    """The per-task workspace manager (a :class:`WorkspaceProvider`) rooted at
    ``root`` on the host (TITAN-SEC-040)."""
    return WorkspaceManager(Path(root))


def build_sandbox_controller(
    image: str, *, docker_host: str = "", egress: EgressConfig | None = None
) -> SandboxController:
    """The Sandbox Controller for hardened per-task code execution (Vol 8 §5).
    Reachable only when a Docker daemon is available; :meth:`SandboxController.ping`
    tells callers to degrade gracefully when it is not. ``egress`` (Phase 23) puts
    the sandbox on the audited egress network instead of deny-all ``none``."""
    return SandboxController(SandboxPolicy(image=image), docker_host=docker_host, egress=egress)


def build_sandbox_manager(controller: SandboxController) -> SandboxSessionManager:
    """The per-task sandbox-session pool the terminal/git/code tools run inside."""
    return SandboxSessionManager(controller)


def build_dind_controller(
    image: str, preloaded_images: list[str], *, docker_host: str = ""
) -> DindController:
    """The per-task rootless-DinD controller for the docker.* tools (Vol 7 §4.3,
    TITAN-SEC-044). Degrades gracefully (TOOL_UNAVAILABLE) when no daemon exists."""
    return DindController(image=image, preloaded_images=preloaded_images, docker_host=docker_host)


def build_dind_manager(controller: DindController) -> DindSessionManager:
    """The per-task DinD-sidecar pool the docker.* tools run against."""
    return DindSessionManager(controller)


def build_browser_controller(image: str, *, docker_host: str = "") -> BrowserController:
    """The per-task headless-Chromium controller for the browser.* tools
    (TITAN-INT-032). Degrades gracefully when no Docker daemon is reachable."""
    return BrowserController(image=image, docker_host=docker_host)


def build_browser_manager(controller: BrowserController) -> BrowserSessionManager:
    """The per-task browser-page pool the browser.* tools drive."""
    return BrowserSessionManager(controller)


async def verify_audit_chain(conn: AsyncConnection, project_id: uuid.UUID) -> int | None:
    """TITAN-INT-024 verification job entry point: the seq of the first
    tampered row in the project's chain, or None when intact."""
    return await _repo.verify_chain(conn, project_id)


__all__ = [
    "APP_PORTS",
    "TOOL_ERROR_CODES",
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalStatus",
    "Artifact",
    "add_approved_host",
    "is_host_approved",
    "list_approved_hosts",
    "remove_approved_host",
    "BrowserController",
    "BrowserServer",
    "BrowserSession",
    "BrowserSessionManager",
    "DbApprovalGate",
    "DindController",
    "DindSession",
    "DindSessionManager",
    "DindSidecar",
    "EgressConfig",
    "EgressHost",
    "ExecResult",
    "ExecutionOutcome",
    "ParamConstraint",
    "ProjectToolPolicy",
    "RateLimiter",
    "Redactor",
    "ResearchClient",
    "ResearchSession",
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
    "build_browser_controller",
    "build_browser_manager",
    "build_bus",
    "build_dind_controller",
    "build_dind_manager",
    "build_registry",
    "build_research_client",
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
