"""The Tool Bus — every agent side-effect passes through here (TITAN-INT-001).

The §4.2 pipeline, exactly:

    1. parse & schema-validate arguments          → SCHEMA_INVALID
    2. authz: agent scopes ∩ tool scopes, project policy, risk gate,
       TITAN-SEC-031 parameter constraints        → PERMISSION_DENIED / …
    3. tool_audit row, status=started (committed before dispatch)
    4. dispatch to the executor
    5. enforce timeout; capture stdout/stderr/artifacts → TIMEOUT
    6. schema-validate the result; pluggable secret redaction → UPSTREAM_ERROR
    7. finalize the audit row, emit the OTel span, return the result envelope

Authorization reuses the Phase-9 RBAC engine's grammar
(:func:`titan.gateway.rbac.permission_granted`) over the caller's resolved
scope set — there is no second permission system. Pre-execution rejections
(steps 1–2) append a single ``error`` audit row so every rejection is audited
(TITAN-SEC-031 acceptance); executed calls get a ``started`` + terminal pair.
The audit write is NOT best-effort: a call whose audit cannot be persisted
fails loudly rather than returning an unaudited result (TITAN-INT-023).
"""

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from jsonschema import FormatChecker
from jsonschema.validators import Draft202012Validator
from opentelemetry import trace
from opentelemetry.trace import Span, StatusCode
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.gateway.rbac import permission_granted
from titan.tools import repository as repo
from titan.tools.allowlists import allowed_for_role
from titan.tools.browser.session import BrowserSession, BrowserSessionManager
from titan.tools.dind.session import DindSession, DindSessionManager
from titan.tools.envelopes import Artifact, ToolCallEnvelope, ToolResultEnvelope
from titan.tools.errors import ToolError, ToolErrorCode
from titan.tools.redaction import Redactor, default_redactor
from titan.tools.registry import (
    ExecutionOutcome,
    ProjectToolPolicy,
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)
from titan.tools.sandbox.session import SandboxSession, SandboxSessionManager
from titan.tools.sandbox.workspace import Workspace
from titan.tools.streaming import ToolOutputStream
from titan.tools.web.session import ResearchClient, ResearchSession

logger = structlog.get_logger("titan.tools.bus")
tracer = trace.get_tracer("titan.tools")

_MAX_SCHEMA_ERRORS = 5
_STDERR_TAIL_CHARS = 4000


@dataclass(frozen=True)
class ApprovalDecision:
    """The risk gate's verdict on an admin-risk call (TITAN-INT-022). The
    detail is surfaced in the PERMISSION_DENIED message so the agent (and the
    dashboard) can reference the approval record."""

    allowed: bool
    detail: str | None = None


# The §4.4 risk gate consulted before dispatching an admin-risk call. The
# production implementation is titan.tools.approvals.DbApprovalGate (wired by
# build_bus); this default denies everything — fail closed.
ApprovalGate = Callable[[ToolCallEnvelope, ToolSpec], Awaitable[ApprovalDecision]]


async def deny_all_approvals(_call: ToolCallEnvelope, _spec: ToolSpec) -> ApprovalDecision:
    return ApprovalDecision(False, "admin-risk tools are not approvable on this bus")


class RateLimiter(Protocol):
    """Per-key rolling rate limit (§4.1 registry rate limits, §4.4 'write
    auto-approved …; rate-limited')."""

    async def allow(self, key: str, limit_per_min: int) -> bool: ...


class WorkspaceProvider(Protocol):
    """Maps a call to its jailed per-task ``/workspace`` (Phase 20). Absent →
    fs.* tools fail closed with TOOL_UNAVAILABLE."""

    def for_call(self, project_id: uuid.UUID, task_id: uuid.UUID | None) -> Workspace: ...


class SlidingWindowRateLimiter:
    """In-process sliding 60s window. Per-process by design for Phase 19 —
    each Tool Host worker enforces its own budget; a shared Redis limiter can
    replace this behind the same protocol when workers scale out."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        # A plain dict (not defaultdict) so a checked-but-empty key is reclaimed
        # rather than accreting forever — one tool+project key per active pair.
        self._hits: dict[str, deque[float]] = {}

    async def allow(self, key: str, limit_per_min: int) -> bool:
        now = self._now()
        cutoff = now - 60.0
        window = self._hits.get(key)
        if window is not None:
            while window and window[0] <= cutoff:
                window.popleft()
            if not window:
                del self._hits[key]  # fully drained → drop the key
                window = None
        count = len(window) if window is not None else 0
        if count >= limit_per_min:
            return False
        if window is None:
            window = self._hits[key] = deque()
        window.append(now)
        return True


@dataclass(frozen=True)
class ToolCaller:
    """The authenticated agent identity behind a call, with its RBAC scopes
    already resolved (by the runtime, via the Phase-9 PermissionResolver)."""

    agent_id: str  # audit label, e.g. "agent.backend_engineer.2"
    role: str  # allowlist row (Vol 8 §4.3)
    project_id: uuid.UUID  # the agent's bound project
    granted_scopes: frozenset[str]
    task_id: uuid.UUID | None = None
    agent_uuid: uuid.UUID | None = None
    initiator_chain: tuple[str, ...] = ()


@dataclass
class _AuditFacts:
    """Everything the audit rows need, computed once up front."""

    args_hash: str
    args_redacted: dict[str, Any]
    initiator_chain: list[str] = field(default_factory=list)


def _pointer(path: Any) -> str:
    parts = [str(p) for p in path]
    return "/" + "/".join(parts) if parts else "(root)"


class ToolBus:
    """One bus per process, built at boot next to the registry."""

    def __init__(
        self,
        registry: ToolRegistry,
        engine: AsyncEngine,
        *,
        redactor: Redactor = default_redactor,
        approval_gate: ApprovalGate = deny_all_approvals,
        rate_limiter: RateLimiter | None = None,
        workspace_provider: WorkspaceProvider | None = None,
        sandbox_manager: SandboxSessionManager | None = None,
        dind_manager: DindSessionManager | None = None,
        browser_manager: BrowserSessionManager | None = None,
        research_client: ResearchClient | None = None,
        redis: "Redis | None" = None,
    ) -> None:
        self._registry = registry
        self._engine = engine
        self._redact = redactor
        self._approval_gate = approval_gate
        self._rate_limiter: RateLimiter = (
            rate_limiter if rate_limiter is not None else SlidingWindowRateLimiter()
        )
        self._workspace_provider = workspace_provider
        self._sandbox_manager = sandbox_manager
        self._dind_manager = dind_manager
        self._browser_manager = browser_manager
        self._research_client = research_client
        self._redis = redis

    async def invoke(
        self,
        call: ToolCallEnvelope,
        caller: ToolCaller,
        *,
        policy: ProjectToolPolicy | None = None,
    ) -> ToolResultEnvelope:
        """Run one tool call through the full pipeline. Never raises for a
        tool-level failure — those come back as ``status='error'`` envelopes
        the agent can read. Only infrastructure failures (e.g. the audit
        ledger being unreachable) propagate."""
        active_policy = policy if policy is not None else ProjectToolPolicy()
        facts = _AuditFacts(
            args_hash=repo.canonical_hash(call.arguments),
            args_redacted=self._redact(call.arguments),
            initiator_chain=[*caller.initiator_chain, caller.agent_id],
        )
        with tracer.start_as_current_span(f"tool.{call.tool}") as span:
            spec = self._registry.get(call.tool)
            self._annotate(span, call, spec)
            try:
                # Step 1 — parse & schema-validate arguments.
                if spec is None:
                    raise ToolError("TOOL_UNAVAILABLE", f"no such tool: {call.tool}")
                self._validate_arguments(spec, call.arguments)
                # Step 2 — authz.
                await self._authorize(spec, call, caller, active_policy)
            except ToolError as exc:
                return await self._finish(span, call, facts, self._rejection(call, exc))
            except Exception as exc:
                # Fail closed: a misbehaving constraint/approval-gate/schema
                # resolver must still yield an audited error envelope, never an
                # unaudited crash. (_finish stays outside this catch — a dead
                # audit ledger DOES propagate, per TITAN-INT-023.)
                logger.warning("tool.pre_dispatch_failed", tool=call.tool, error=str(exc))
                rejection = self._error_envelope(
                    call, "UPSTREAM_ERROR", f"tool pipeline failed pre-dispatch: {exc}", None
                )
                return await self._finish(span, call, facts, rejection)

            # Step 3 — STARTED audit row, committed before dispatch so a crash
            # mid-execution still leaves evidence of the attempt.
            async with self._engine.begin() as conn:
                await repo.append_audit(
                    conn,
                    tool_call_id=call.tool_call_id,
                    trace_id=call.trace_id,
                    project_id=call.project_id,
                    task_id=call.task_id,
                    agent_id=call.agent_id,
                    tool=call.tool,
                    args_hash=facts.args_hash,
                    args_redacted=facts.args_redacted,
                    status="started",
                    initiator_chain=facts.initiator_chain,
                )

            # Steps 4–6 — dispatch, timeout, capture, validate, redact.
            envelope = await self._execute(spec, call, caller)
            # Step 7 — finalize audit, annotate span, return.
            return await self._finish(span, call, facts, envelope)

    # -- pipeline steps -------------------------------------------------------

    def _validate_arguments(self, spec: ToolSpec, arguments: dict[str, Any]) -> None:
        validator = Draft202012Validator(spec.input_schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
        if not errors:
            return
        shown = [f"at '{_pointer(e.absolute_path)}': {e.message}" for e in errors]
        detail = "; ".join(shown[:_MAX_SCHEMA_ERRORS])
        if len(shown) > _MAX_SCHEMA_ERRORS:
            detail += f" (+{len(shown) - _MAX_SCHEMA_ERRORS} more)"
        raise ToolError("SCHEMA_INVALID", f"arguments failed schema validation: {detail}")

    async def _authorize(
        self,
        spec: ToolSpec,
        call: ToolCallEnvelope,
        caller: ToolCaller,
        policy: ProjectToolPolicy,
    ) -> None:
        # Attribution must match the authenticated caller — envelopes are
        # model-generated and never trusted for identity.
        if call.agent_id != caller.agent_id:
            raise ToolError("PERMISSION_DENIED", "call agent_id does not match the caller")
        # TITAN-SEC-031: task project scope — the call stays inside the
        # agent's bound project and current task.
        if call.project_id != caller.project_id:
            raise ToolError("PERMISSION_DENIED", "call is outside the agent's bound project")
        # Strict task binding: the envelope's task must BE the caller's task
        # (both None for untasked work). A tasked caller may not shed its task
        # attribution, and an untasked caller may not claim one.
        if call.task_id != caller.task_id:
            raise ToolError("PERMISSION_DENIED", "call is outside the agent's current task")
        # Role allowlist (Vol 8 §4.3): out-of-list calls rejected regardless of
        # what the model asked for.
        if not allowed_for_role(caller.role, spec.name):
            raise ToolError(
                "PERMISSION_DENIED", f"tool {spec.name} is not in the {caller.role} role allowlist"
            )
        # Project policy (third §4.1 intersection term).
        if not policy.provider_enabled(spec.provider) or not policy.tool_enabled(spec.name):
            raise ToolError("TOOL_UNAVAILABLE", f"tool {spec.name} is disabled by project policy")
        # Agent scopes ∩ tool scopes, via the Phase-9 RBAC grammar.
        for scope in spec.required_scopes:
            if not permission_granted(scope, set(caller.granted_scopes)):
                raise ToolError("PERMISSION_DENIED", f"missing required scope: {scope}")
        # §4.1/§4.4 registry rate limits (checked BEFORE the approval gate so a
        # looping agent burns its rate budget, not the approvals dashboard).
        if spec.rate_limit_per_min is not None:
            key = f"{spec.name}:{call.project_id}"
            if not await self._rate_limiter.allow(key, spec.rate_limit_per_min):
                raise ToolError(
                    "TOOL_UNAVAILABLE",
                    f"tool {spec.name} rate limit exceeded "
                    f"({spec.rate_limit_per_min}/min in this project); retry later",
                )
        # Risk gate (§4.4): read/write are auto-approved (within the project
        # scope enforced above); admin blocks on an approval record
        # (TITAN-INT-022) — the gate's detail names the record for the
        # dashboard and the agent's retry.
        if spec.risk == "admin":
            decision = await self._approval_gate(call, spec)
            if not decision.allowed:
                suffix = f": {decision.detail}" if decision.detail else ""
                raise ToolError(
                    "PERMISSION_DENIED",
                    f"tool {spec.name} is admin-risk and requires approval{suffix}",
                )
        # TITAN-SEC-031 per-parameter rules (e.g. git writes only to
        # titan/task-* branches).
        for constraint in spec.constraints:
            violation = constraint(call.arguments)
            if violation is not None:
                code, message = violation
                raise ToolError(code, message)

    async def _execute(
        self, spec: ToolSpec, call: ToolCallEnvelope, caller: ToolCaller
    ) -> ToolResultEnvelope:
        workspace = (
            self._workspace_provider.for_call(call.project_id, call.task_id)
            if self._workspace_provider is not None
            else None
        )
        # A lightweight per-call session handle (the container is created lazily
        # only if a terminal/git/code tool actually calls `await ctx.sandbox.get()`).
        sandbox: SandboxSession | None = None
        if self._sandbox_manager is not None and workspace is not None:
            stream = ToolOutputStream(
                self._redis,
                project_id=call.project_id,
                task_id=call.task_id,
                agent_id=call.agent_id,
                tool=call.tool,
                tool_call_id=call.tool_call_id,
            )
            sandbox = SandboxSession(
                self._sandbox_manager,
                project_id=call.project_id,
                task_id=call.task_id,
                workspace=workspace,
                stream=stream,
            )
        # docker.* run against a per-task DinD sidecar (needs the workspace for
        # compose files / build contexts); browser.* drive a per-task page.
        dind: DindSession | None = None
        if self._dind_manager is not None and workspace is not None:
            dind = DindSession(
                self._dind_manager,
                project_id=call.project_id,
                task_id=call.task_id,
                workspace=workspace,
            )
        browser: BrowserSession | None = None
        if self._browser_manager is not None:
            browser = BrowserSession(
                self._browser_manager, project_id=call.project_id, task_id=call.task_id
            )
        # web.* run in the trusted tool host — no workspace/sandbox needed, just
        # the boot-time research gateway client wrapped per call.
        research = (
            ResearchSession(self._research_client) if self._research_client is not None else None
        )
        context = ToolExecutionContext(
            engine=self._engine,
            project_id=call.project_id,
            task_id=call.task_id,
            agent_id=call.agent_id,
            agent_uuid=caller.agent_uuid,
            workspace=workspace,
            sandbox=sandbox,
            dind=dind,
            browser=browser,
            research=research,
        )
        started = time.monotonic()
        try:
            outcome = await asyncio.wait_for(
                spec.executor(call.arguments, context), timeout=spec.timeout_s
            )
            self._validate_result(spec, outcome)
            # Envelope shaping stays INSIDE the try: a malformed artifact dict
            # or non-str stderr from an ill-behaved executor must become an
            # UPSTREAM_ERROR envelope (and a terminal audit row), not an
            # exception escaping invoke(). Artifacts pass through the redactor
            # like result/stderr — paths and names can carry secrets too.
            duration_ms = int((time.monotonic() - started) * 1000)
            return ToolResultEnvelope(
                tool_call_id=call.tool_call_id,
                status="ok",
                duration_ms=duration_ms,
                result=self._redact(outcome.result),
                stderr_tail=self._redact(str(outcome.stderr_tail or "")[-_STDERR_TAIL_CHARS:]),
                artifacts=[Artifact.model_validate(self._redact(a)) for a in outcome.artifacts],
            )
        except TimeoutError:
            return self._error_envelope(
                call,
                "TIMEOUT",
                f"tool {spec.name} exceeded its {spec.timeout_s:g}s timeout",
                started,
            )
        except ToolError as exc:
            return self._error_envelope(call, exc.code, exc.message, started)
        except Exception as exc:
            logger.warning("tool.executor_failed", tool=spec.name, error=str(exc))
            return self._error_envelope(
                call, "UPSTREAM_ERROR", f"tool {spec.name} failed: {exc}", started
            )

    def _validate_result(self, spec: ToolSpec, outcome: ExecutionOutcome) -> None:
        validator = Draft202012Validator(spec.output_schema, format_checker=FormatChecker())
        errors = sorted(validator.iter_errors(outcome.result), key=lambda e: list(e.absolute_path))
        if errors:
            first = errors[0]
            raise ToolError(
                "UPSTREAM_ERROR",
                f"tool returned a result violating its output schema "
                f"at '{_pointer(first.absolute_path)}': {first.message}",
            )

    def _rejection(self, call: ToolCallEnvelope, exc: ToolError) -> ToolResultEnvelope:
        return self._error_envelope(call, exc.code, exc.message, None)

    def _error_envelope(
        self,
        call: ToolCallEnvelope,
        code: ToolErrorCode,
        message: str,
        started: float | None,
    ) -> ToolResultEnvelope:
        duration_ms = 0 if started is None else int((time.monotonic() - started) * 1000)
        # Executor exception text can quote secrets (DSNs, headers) — redact
        # before it reaches the LLM prompt or the span status.
        return ToolResultEnvelope(
            tool_call_id=call.tool_call_id,
            status="error",
            duration_ms=duration_ms,
            code=code,
            message=str(self._redact(message)),
        )

    async def _finish(
        self,
        span: Span,
        call: ToolCallEnvelope,
        facts: _AuditFacts,
        envelope: ToolResultEnvelope,
    ) -> ToolResultEnvelope:
        """Step 7: append the terminal audit row and annotate the span."""
        result_hash = repo.canonical_hash(envelope.result) if envelope.status == "ok" else None
        async with self._engine.begin() as conn:
            await repo.append_audit(
                conn,
                tool_call_id=call.tool_call_id,
                trace_id=call.trace_id,
                project_id=call.project_id,
                task_id=call.task_id,
                agent_id=call.agent_id,
                tool=call.tool,
                args_hash=facts.args_hash,
                args_redacted=facts.args_redacted,
                status=envelope.status,
                initiator_chain=facts.initiator_chain,
                error_code=envelope.code,
                duration_ms=envelope.duration_ms,
                result_hash=result_hash,
            )
        span.set_attribute("titan.tool.status", envelope.status)
        span.set_attribute("titan.tool.duration_ms", envelope.duration_ms)
        if envelope.code is not None:
            span.set_attribute("titan.tool.error_code", envelope.code)
            span.set_status(StatusCode.ERROR, envelope.message or envelope.code)
        logger.info(
            "tool.invoked",
            tool=call.tool,
            tool_call_id=call.tool_call_id,
            status=envelope.status,
            error_code=envelope.code,
            duration_ms=envelope.duration_ms,
            project_id=str(call.project_id),
            agent_id=call.agent_id,
        )
        return envelope

    def _annotate(self, span: Span, call: ToolCallEnvelope, spec: ToolSpec | None) -> None:
        # Never put arguments or results on the span — secrets (TITAN-OPS-016).
        span.set_attribute("titan.tool", call.tool)
        span.set_attribute("titan.tool_call_id", call.tool_call_id)
        span.set_attribute("titan.project_id", str(call.project_id))
        span.set_attribute("titan.agent_id", call.agent_id)
        if call.task_id is not None:
            span.set_attribute("titan.task_id", str(call.task_id))
        if spec is not None:
            span.set_attribute("titan.tool.provider", spec.provider)
            span.set_attribute("titan.tool.risk", spec.risk)


__all__ = [
    "ApprovalDecision",
    "ApprovalGate",
    "RateLimiter",
    "SlidingWindowRateLimiter",
    "ToolBus",
    "ToolCaller",
    "WorkspaceProvider",
    "deny_all_approvals",
]
