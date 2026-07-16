"""LiveExecutor — the real agent turn (Vol 2 ``run_turn``) as the delivery run
executor (Phase 56, closing the StubExecutor gap of Vol 3 §8.2).

A scheduled task run now executes a REAL agent turn through the shared runtime
(the LLM gateway → Ollama on the offline profile) instead of the canned
:class:`~titan.runs.executor.StubExecutor`. It maps ``RunContext`` → ``run_turn``
→ ``RunOutcome``; the coordinator still owns persistence, timeout, and the state
machine (no coordinator change).

Two honest details:

* ``run_turn`` is inbox/outbox driven and returns metadata only — it EMITS
  ``AgentMessage``s, it does not return artifact bytes. So the outcome always
  carries a ``build_log`` artifact summarizing the turn, plus best-effort
  workspace files when a sandbox produced them.
* A turn that did not ``complete`` (escalated, failed-open, invalid output,
  unknown role) maps to a FAILED ``RunOutcome`` — recorded as-is, never massaged
  into success. A local 7B will fail often; that is the truthful signal.
"""

import contextlib
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Cross-module imports go through the public `.service` surfaces so the
# domain-independence contract (TITAN-BCK-001/004) stays satisfied.
from titan.agents.service import CORE_ROSTER, roster_id_for_role
from titan.messaging.service import AgentMessage
from titan.runs.executor import ExecutedArtifact, RunContext, RunOutcome
from titan.runtime.service import AgentRuntime, TurnResult, run_turn

#: Resolve the run's UUID agent-instance id to its roster role id (e.g.
#: ``eng_backend``). Injected so the executor stays free of a direct DB coupling
#: and is trivially faked in tests.
RoleResolver = Callable[[uuid.UUID], Awaitable[str | None]]

#: Best-effort collector of files a turn wrote to its workspace/sandbox, returned
#: as artifacts. Absent (worker has no sandbox) → the build_log is the only artifact.
WorkspaceCollector = Callable[[RunContext], Awaitable[list[ExecutedArtifact]]]

_DEFAULT_CRITERION = "Complete the task per the spec and the acceptance gate."
#: Namespace for deriving the trigger's ids DETERMINISTICALLY from the stable
#: run_id. A run re-executed after a soft-timeout/reclaim (same run_id) must build
#: the SAME trigger id so run_turn's idempotent replay (keyed on message_id) fires
#: and does not re-emit messages or re-run tool side effects (TITAN-AGT-002).
_TRIGGER_NS = uuid.uuid5(uuid.NAMESPACE_URL, "titan.runs.live_executor.trigger")


class LiveExecutor:
    """Runs one real agent turn per task attempt and reports its honest outcome."""

    def __init__(
        self,
        runtime: AgentRuntime,
        resolve_role: RoleResolver,
        *,
        collect_workspace: WorkspaceCollector | None = None,
    ) -> None:
        self._runtime = runtime
        self._resolve_role = resolve_role
        self._collect_workspace = collect_workspace

    async def execute(self, ctx: RunContext) -> RunOutcome:
        role = await self._resolve_role(ctx.agent_id)
        if role is None or role not in CORE_ROSTER:
            return _failed_outcome(
                ctx, error=f"agent {ctx.agent_id} maps to no roster role ({role!r})"
            )
        trigger = _build_trigger(ctx, role)
        result = await run_turn(self._runtime, ctx.project_id, role, trigger)
        return await self._to_outcome(ctx, result)

    async def _to_outcome(self, ctx: RunContext, result: TurnResult) -> RunOutcome:
        artifacts = [_turn_log(ctx, result)]
        if self._collect_workspace is not None:
            # The build_log stands alone; a workspace-collection failure never fails the run.
            with contextlib.suppress(Exception):
                artifacts.extend(await self._collect_workspace(ctx))
        # A fail-open role (docs/critic, §4.13/§4.16) returns ``failed_open`` BY
        # DESIGN so the pipeline proceeds — mapping it to a delivery-run failure
        # would invert fail-open into a task failure. So "proceed" = completed OR
        # failed_open; everything else (escalated/error/unknown_agent) fails.
        proceed = result.outcome in ("completed", "failed_open")
        error = None
        if not proceed:
            error = str(result.failure_reason) if result.failure_reason else result.outcome
        return RunOutcome(
            status="succeeded" if proceed else "failed",
            output_summary=_summary(result),
            provider=result.provider or "unknown",
            model=result.model_alias or "unknown",
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
            # The run-row cost is 0 for the offline (Ollama, free) profile. The
            # AUTHORITATIVE per-call cost is written to llm_usage by the gateway for
            # EVERY provider, and the KPI cost ledger reads llm_usage — not this
            # denormalized field — so a non-Ollama run's real cost is never lost here.
            cost_usd=Decimal("0.0000"),
            error=error,
            artifacts=artifacts,
        )


def _build_trigger(ctx: RunContext, role: str) -> AgentMessage:
    spec = ctx.spec or f"task {ctx.task_id}"
    criteria = ctx.input.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        criteria = [_DEFAULT_CRITERION]
    # Deterministic ids from the stable run_id → a re-executed run replays.
    trigger_id = uuid.uuid5(_TRIGGER_NS, str(ctx.run_id))
    return AgentMessage(
        message_id=trigger_id,
        correlation_id=uuid.uuid5(_TRIGGER_NS, f"corr:{ctx.run_id}"),
        type="TaskAssignment",
        project_id=ctx.project_id,
        task_id=ctx.task_id,
        from_agent="orchestrator",
        to_agent=role,
        payload={
            "title": spec[:200],
            "description": spec,
            "acceptance_criteria": [str(c) for c in criteria],
        },
    )


def _summary(result: TurnResult) -> str:
    parts = [
        result.outcome,
        f"{len(result.emitted_message_ids)} msgs",
        f"{len(result.tool_calls)} tools",
    ]
    if result.failure_reason:
        parts.append(f"failure={result.failure_reason}")
    return ", ".join(parts)


def _turn_log(ctx: RunContext, result: TurnResult) -> ExecutedArtifact:
    lines = [
        f"# Agent turn — run {ctx.run_id} attempt {ctx.attempt}",
        f"role: {result.agent_id}",
        f"outcome: {result.outcome}",
        f"messages emitted: {len(result.emitted_message_ids)}",
        f"tool calls: {len(result.tool_calls)}",
        f"recovery rungs: {', '.join(result.recovery_rungs) or 'none'}",
        f"model: {result.provider or '?'}/{result.model_alias or '?'}",
        f"tokens: in={result.input_tokens} out={result.output_tokens}",
    ]
    if result.failure_reason:
        lines.append(f"failure_reason: {result.failure_reason}")
    if result.escalation_target:
        lines.append(f"escalated_to: {result.escalation_target}")
    return ExecutedArtifact(
        kind="build_log", data="\n".join(lines).encode(), path=f"runs/{ctx.run_id}/turn.log"
    )


def make_role_resolver(engine: AsyncEngine) -> RoleResolver:
    """A resolver that reads the agent-instance row's ``role`` and maps it to the
    roster runtime id (Phase 56). An unknown/unmapped agent → None (the executor
    then fails the run rather than letting ``run_turn`` see an unknown role)."""

    async def resolve(agent_id: uuid.UUID) -> str | None:
        async with engine.connect() as conn:
            row = (
                await conn.execute(text("SELECT role FROM agents WHERE id = :id"), {"id": agent_id})
            ).first()
        return roster_id_for_role(str(row.role)) if row is not None else None

    return resolve


def _failed_outcome(ctx: RunContext, *, error: str) -> RunOutcome:
    return RunOutcome(
        status="failed",
        output_summary=error,
        provider="none",
        model="none",
        tokens_in=0,
        tokens_out=0,
        cost_usd=Decimal("0.0000"),
        error=error,
        artifacts=[
            ExecutedArtifact(
                kind="build_log", data=error.encode(), path=f"runs/{ctx.run_id}/turn.log"
            )
        ],
    )


__all__ = ["LiveExecutor", "RoleResolver", "WorkspaceCollector", "make_role_resolver"]
