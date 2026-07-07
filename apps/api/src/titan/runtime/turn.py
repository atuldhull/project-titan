"""The stateless agent turn (Vol 2 §8, TITAN-AGT-002/004/023/024).

An "agent" is not a process — it exists only during a **turn**: one
hydrate → call → act cycle the shared runtime runs when a message lands in the
agent's inbox. The seven steps of §8, in order:

    1. (worker) XREADGROUP one message from the agent's inbox stream.
    2. Load the cached ``AgentDefinition`` (Phase 26 registry).
    3. Hydrate context: composed system prompt (Phase 28), the thread's messages
       from PostgreSQL, a Memory Packet (Vol 5 §13 hook), and the schemas of
       GRANTED tools only.
    4. Call the LLM through the gateway, routed by model-policy (Phase 28 binding)
       — never a provider SDK.
    5. Validate the structured output (Pydantic). Invalid output is never
       silently accepted — it drives the self-critique retry rung (TITAN-AGT-023).
    6. Execute granted tool calls through the Tool Bus (audited, TITAN-AGT-024);
       reject ungranted ones as security events (TITAN-AGT-004); emit the turn's
       messages via the outbox (Postgres → Redis).
    7. (worker) XACK + OTel span + KPI metrics.

Durability (TITAN-AGT-002): the turn holds no state a crash could lose — the
definition is reloadable and hydration reads PostgreSQL. Emission is idempotent by
construction: a turn's outbound messages carry deterministic ids, and because every
turn emits at least one message (§2.2, enforced by ``AgentTurnOutput``) which commit
atomically, a reprocessed turn (crash after emit, before XACK) always detects the
prior attempt via :func:`_already_emitted` and SHORT-CIRCUITS — re-running neither
the (possibly non-deterministic) LLM nor its tools, and re-emitting nothing. So
kill/restart mid-exchange loses no accepted work and duplicates no message, whether
or not the provider is deterministic.

A crash BEFORE emission commits (during hydration, the LLM, or tool execution)
leaves no marker, so the turn re-runs in full — losing nothing, but re-executing
any tools it had already called. Tool execution is therefore at-least-once across a
mid-turn crash: the deliberate semantics of the at-least-once Tool Bus (every call
is audited; tools are idempotent or their retries handled by the Phase-30 ladder),
not a gap. Once a turn has committed its messages, though, its whole effect —
messages and tools — is never re-run.
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

import structlog
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.agents.service import (
    AgentDefinition,
    AgentRegistry,
    PromptComposer,
    compose_system_prompt,
)
from titan.conflict.service import capture_veto_verdict
from titan.llm.service import (
    ChatRequest,
    ChatResponse,
    ContentPart,
    Message,
    TextPart,
    ToolDef,
    ToolResultPart,
    ToolUsePart,
)
from titan.messaging.service import (
    AgentMessage,
    MessageOut,
    get_message,
    message_exists,
    messages_in_thread,
    send_agent_message,
)
from titan.runtime import metrics
from titan.runtime.binding import GatewayBinding, LlmClient
from titan.runtime.grants import (
    AGENT_ROLE,
    SecurityEvent,
    granted_scopes,
    granted_tool_defs,
    is_granted,
    log_security_event,
)
from titan.runtime.memory import MemoryProvider, MemoryRequest
from titan.runtime.output import AgentTurnOutput, TurnOutputError, parse_turn_output
from titan.runtime.recovery import (
    RUNG_ESCALATE,
    RUNG_SELF_CRITIQUE,
    RUNG_SUPERVISOR,
    RUNG_TRANSIENT,
    LadderConfig,
    build_escalation,
    is_blocked_escalation,
    is_transient,
    turn_operation_label,
)
from titan.tools.service import (
    ProjectToolPolicy,
    ToolBus,
    ToolCaller,
    ToolRegistry,
    ToolResultEnvelope,
    new_call,
)

logger = structlog.get_logger("titan.runtime.turn")
_TRACER = trace.get_tracer("titan.runtime")

# Namespace for deterministic outbound message ids: uuid5(ns, "<trigger>:<seq>").
# A reprocessed turn (crash before XACK) re-derives the same ids, so re-emission
# is a no-op — no duplicate side effects (TITAN-AGT-002/016).
RUNTIME_MSG_NAMESPACE = uuid.UUID("6f9c1e2a-4d3b-5a1c-9e7f-2b8a0c1d4e6f")

TurnOutcome = Literal[
    "completed",  # a valid output was produced and its messages emitted
    "escalated",  # rungs 1–2 exhausted → a blocked report went to the supervisor/human
    "failed_open",  # a fail-open agent (docs/critic) failed — logged, never escalated
    "unknown_agent",  # no such role in the roster (undeliverable)
    "error",  # an infrastructure error — transient, safe to reprocess
]

# Why a turn could not complete (recorded even when the ladder then escalates).
FailureReason = Literal["invalid_output", "tool_budget_exhausted", "transient_error"]

# Bounds on one turn's LLM round-trips (§8 step 4/5). The tool-use loop lets the
# model act then react; the self-critique + transient-retry rungs re-invoke.
DEFAULT_MAX_TOOL_ITERATIONS = 4
DEFAULT_MAX_SELF_CRITIQUE = 2
DEFAULT_MAX_TRANSIENT_RETRIES = 2
DEFAULT_BACKOFF_BASE_S = 0.5


@dataclass
class ToolCallRecord:
    """One model-requested tool call the turn saw, and what became of it."""

    tool: str
    disposition: Literal["granted", "denied"]
    status: str | None = None  # "ok" | "error" for granted calls; None for denied


@dataclass
class TurnResult:
    """The outcome of one turn — the worker's ack decision and the KPI facts.

    Carries metadata only (ids, counts, model routing) — never prompt/completion
    or message content (TITAN-OPS-016)."""

    outcome: TurnOutcome
    agent_id: str
    trigger_message_id: uuid.UUID
    packet_id: uuid.UUID | None = None
    model_alias: str | None = None
    provider: str | None = None
    emitted_message_ids: list[uuid.UUID] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    security_events: list[SecurityEvent] = field(default_factory=list)
    output_retries: int = 0
    transient_retries: int = 0
    # The rungs (rung1..rung4) the recovery ladder fired this turn, in order.
    recovery_rungs: list[str] = field(default_factory=list)
    failure_reason: FailureReason | None = None
    escalation_target: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    @property
    def completed(self) -> bool:
        return self.outcome == "completed"

    @property
    def should_ack(self) -> bool:
        """Ack every terminal outcome — only a raw infrastructure ``error`` is
        left pending for the reaper to reclaim and retry (at-least-once). A
        handled-but-failed turn (escalated, failed-open, unknown agent) is acked
        so it does not poison-loop; the ladder has already escalated/logged it."""
        return self.outcome != "error"


@dataclass
class AgentRuntime:
    """The shared runtime's dependencies — built once, reused across turns."""

    engine: AsyncEngine
    registry: AgentRegistry
    composer: PromptComposer
    tool_registry: ToolRegistry
    tool_bus: ToolBus
    llm: LlmClient
    binding: GatewayBinding
    memory: MemoryProvider
    # Roles holding a §7.4 blocking veto (``{"security"}`` by default) — when one of
    # them emits a veto verdict, the runtime records the pipeline veto (TITAN-AGT-018).
    vetoing_roles: frozenset[str] = frozenset()
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS
    max_self_critique: int = DEFAULT_MAX_SELF_CRITIQUE
    max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES
    # Exponential-backoff base for rung-1 transient retries; tests set 0 for speed.
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S


# --- hydration helpers -----------------------------------------------------


def _render_thread_context(thread: list[MessageOut], trigger: AgentMessage) -> str:
    """Render the thread transcript + the triggering message into the prompt's
    task/thread slot (§8 slot 7). This is prompt INPUT — it never reaches a log
    or span. The trigger is shown explicitly and excluded from the transcript so
    it is not duplicated."""
    lines: list[str] = []
    prior = [m for m in thread if m.id != trigger.message_id]
    if prior:
        lines.append("Conversation so far (oldest first):")
        for m in prior:
            sender = m.from_agent or (str(m.sender_user_id) if m.sender_user_id else "user")
            lines.append(f"- [{m.kind}] {sender} -> {m.to_agent or '-'}: {json.dumps(m.body)}")
        lines.append("")
    lines.append(f"Act now on this {trigger.type} from {trigger.from_agent}:")
    lines.append(json.dumps(trigger.payload))
    return "\n".join(lines)


def _tool_result_content(envelope: ToolResultEnvelope) -> tuple[str, bool]:
    """A model-readable rendering of a tool result + whether it was an error."""
    if envelope.status == "ok":
        body: dict[str, object] = {"result": envelope.result}
        if envelope.stderr_tail:
            body["stderr_tail"] = envelope.stderr_tail
        return json.dumps(body), False
    return (envelope.message or envelope.code or "tool error"), True


# --- the turn --------------------------------------------------------------


async def run_turn(
    runtime: AgentRuntime,
    project_id: uuid.UUID,
    agent_id: str,
    trigger: AgentMessage,
) -> TurnResult:
    """Execute one full agent turn for ``trigger`` (§8 steps 2–7 — the worker
    owns steps 1 and the final XACK)."""
    started = time.monotonic()
    with _TRACER.start_as_current_span("agent.turn") as span:
        span.set_attribute("titan.agent_id", agent_id)
        span.set_attribute("titan.project_id", str(project_id))
        span.set_attribute("titan.message.type", trigger.type)
        span.set_attribute("titan.correlation_id", str(trigger.correlation_id))
        if trigger.task_id is not None:
            span.set_attribute("titan.task_id", str(trigger.task_id))

        # Step 2 — the cached role contract.
        definition = runtime.registry.get(agent_id)
        if definition is None:
            logger.warning("runtime.unknown_agent", agent_id=agent_id)
            result = TurnResult("unknown_agent", agent_id, trigger.message_id)
            _finish_span(span, result, started)
            return result

        try:
            result = await _execute(runtime, project_id, agent_id, trigger, definition, span)
        except Exception:  # noqa: BLE001 — an infra failure is transient; reclaim + retry
            logger.warning("runtime.turn_error", agent_id=agent_id, exc_info=True)
            result = TurnResult("error", agent_id, trigger.message_id)

        _finish_span(span, result, started)
        result.latency_ms = int((time.monotonic() - started) * 1000)
        _emit_kpis(result)
        return result


def _finish_span(span: trace.Span, result: TurnResult, started: float) -> None:
    span.set_attribute("titan.turn.outcome", result.outcome)
    span.set_attribute("titan.turn.tool_calls", len(result.tool_calls))
    span.set_attribute("titan.turn.security_events", len(result.security_events))
    span.set_attribute("titan.turn.output_retries", result.output_retries)
    span.set_attribute("titan.turn.transient_retries", result.transient_retries)
    span.set_attribute("titan.turn.messages_emitted", len(result.emitted_message_ids))
    if result.recovery_rungs:
        span.set_attribute("titan.recovery.rungs", ",".join(result.recovery_rungs))
    if result.escalation_target is not None:
        span.set_attribute("titan.recovery.escalation_target", result.escalation_target)
    if result.failure_reason is not None:
        span.set_attribute("titan.recovery.failure_reason", result.failure_reason)
    if result.packet_id is not None:
        span.set_attribute("titan.memory.packet_id", str(result.packet_id))
    if result.model_alias is not None:
        span.set_attribute("titan.llm.alias", result.model_alias)
    if result.provider is not None:
        span.set_attribute("gen_ai.system", result.provider)


def _emit_kpis(result: TurnResult) -> None:
    metrics.observe_turn(
        agent=f"agent.{result.agent_id}",
        outcome=result.outcome,
        latency_seconds=result.latency_ms / 1000.0,
        granted_tool_calls=sum(1 for t in result.tool_calls if t.disposition == "granted"),
        denied_tool_calls=len(result.security_events),
        output_retries=result.output_retries,
        messages_emitted=len(result.emitted_message_ids),
    )


def _record_rung(
    result: TurnResult, agent_id: str, rung: str, *, attempt: int | None = None
) -> None:
    """Emit one OTel span for a fired recovery-ladder rung + its KPI metric, and
    record it on the result. Metadata only — no prompt/message content."""
    span = _TRACER.start_span(f"recovery.{rung}")
    span.set_attribute("titan.recovery.rung", rung)
    span.set_attribute("titan.agent_id", agent_id)
    if attempt is not None:
        span.set_attribute("titan.recovery.attempt", attempt)
    span.end()
    # Every rung is LOGGED and traced (§2.2) — a uniform structured line so an
    # operator can grep any rung (rungs 1/3 add their own detail line too).
    logger.info("recovery.rung", agent_id=agent_id, rung=rung, attempt=attempt)
    result.recovery_rungs.append(rung)
    metrics.observe_rung(f"agent.{agent_id}", rung)


async def _execute(
    runtime: AgentRuntime,
    project_id: uuid.UUID,
    agent_id: str,
    trigger: AgentMessage,
    definition: AgentDefinition,
    span: trace.Span,
) -> TurnResult:
    result = TurnResult("error", agent_id, trigger.message_id)

    # Idempotent replay (TITAN-AGT-002): if a prior attempt already committed this
    # turn's emission (crash after emit, before XACK), the deterministic ids are
    # already in `messages`. Emission commits atomically, so index 0 existing means
    # the WHOLE set exists — re-derive it and short-circuit, re-running neither the
    # LLM nor its tools. This makes a completed-but-unacked turn a true no-op on
    # reprocess even when the provider is non-deterministic, so no message (and no
    # tool side effect of an already-completed turn) is ever duplicated.
    already = await _already_emitted(runtime, trigger)
    if already is not None:
        result.emitted_message_ids = already
        # Recover the prior attempt's REAL outcome from what it emitted: a lone
        # blocked ProgressReport was a rung-3 escalation, not a plain completion —
        # otherwise the replay would count a failure as a success (KPI corruption).
        result.outcome, result.escalation_target = await _replayed_outcome(runtime, already)
        return result

    # Rung 4: this agent is a supervisor handling a subordinate's blocked report;
    # its normal output (reassign / decompose / escalate-to-human) IS the action.
    if is_blocked_escalation(trigger):
        _record_rung(result, agent_id, RUNG_SUPERVISOR)

    ladder = LadderConfig.from_definition(
        definition,
        max_transient_retries=runtime.max_transient_retries,
        max_self_critique=runtime.max_self_critique,
        backoff_base_s=runtime.backoff_base_s,
    )
    # The Database Engineer's destructive migrations skip rungs 1–2 (§4.8): a
    # failure escalates immediately rather than retrying an irreversible op.
    skip = ladder.skips_retries(turn_operation_label(trigger))

    # Step 3 — hydrate.
    thread_id = trigger.thread_id or trigger.correlation_id
    async with runtime.engine.connect() as conn:
        thread = await messages_in_thread(conn, project_id, thread_id)
    packet = await runtime.memory.assemble(
        MemoryRequest(
            agent_id=agent_id,
            project_id=project_id,
            task_id=trigger.task_id,
            call_seq=len(thread),
            budget_tokens=definition.model_policy.max_output_tokens,
        )
    )
    result.packet_id = packet.packet_id
    system = compose_system_prompt(
        runtime.composer,
        definition,
        task_context=_render_thread_context(thread, trigger),
        memory=packet.render(),
    )
    tool_defs = granted_tool_defs(runtime.tool_registry, definition.tool_grants)
    caller = ToolCaller(
        agent_id=f"agent.{agent_id}",
        role=AGENT_ROLE.get(agent_id, ""),
        project_id=project_id,
        granted_scopes=granted_scopes(runtime.tool_registry, definition.tool_grants),
        task_id=trigger.task_id,
    )

    convo: list[Message] = [
        Message(
            role="user",
            content=(
                "Produce your response as a single JSON object matching AgentTurnOutput "
                "(a 'messages' array of typed §6.2 messages, plus an optional 'note'). "
                "If you must use a granted tool first, call it; otherwise return the JSON."
            ),
        )
    ]

    # Steps 4–5 — the tool-use loop with rung-1 transient retry + rung-2 self-critique.
    try:
        output = await _agent_loop(
            runtime,
            agent_id,
            project_id,
            trigger,
            definition,
            ladder,
            skip,
            system,
            convo,
            tool_defs,
            caller,
            result,
        )
    except Exception as exc:  # noqa: BLE001
        if not is_transient(exc):
            raise  # terminal/infra error → run_turn marks "error" (reclaim + retry)
        # Rung 1 budget spent and the error is still transient → escalate (blocked).
        result.failure_reason = "transient_error"
        logger.warning("runtime.transient_exhausted", agent_id=agent_id, error=type(exc).__name__)
        output = None

    if output is not None:
        # Step 6 — emit the turn's messages idempotently.
        result.emitted_message_ids = await _emit_messages(
            runtime, project_id, agent_id, trigger, output
        )
        result.outcome = "completed"
        return result

    # Rungs 1–2 exhausted → rung 3 escalation (or a fail-open log).
    return await _escalate(runtime, project_id, agent_id, trigger, ladder, result)


async def _llm_call(
    runtime: AgentRuntime,
    agent_id: str,
    ladder: LadderConfig,
    skip: bool,
    request: ChatRequest,
    result: TurnResult,
) -> ChatResponse:
    """Rung 1 of the ladder: call the gateway, retrying a TRANSIENT failure with
    the same context (exponential backoff, bounded). A non-transient error (bad
    request, config, budget) is not retried — it propagates to escalate/reclaim."""
    max_transient = 0 if skip else ladder.max_transient_retries
    attempt = 0
    while True:
        try:
            return await runtime.llm.chat(request)
        except Exception as exc:  # noqa: BLE001
            if not is_transient(exc) or attempt >= max_transient:
                raise
            attempt += 1
            result.transient_retries += 1
            _record_rung(result, agent_id, RUNG_TRANSIENT, attempt=attempt)
            logger.warning(
                "recovery.rung1_transient_retry",
                agent_id=agent_id,
                attempt=attempt,
                error=type(exc).__name__,
            )
            await asyncio.sleep(ladder.backoff_delay(attempt - 1))


async def _agent_loop(
    runtime: AgentRuntime,
    agent_id: str,
    project_id: uuid.UUID,
    trigger: AgentMessage,
    definition: AgentDefinition,
    ladder: LadderConfig,
    skip: bool,
    system: str,
    convo: list[Message],
    tool_defs: list[ToolDef],
    caller: ToolCaller,
    result: TurnResult,
) -> AgentTurnOutput | None:
    """Drive the LLM: execute granted tool calls and react (bounded), and validate
    the final structured output with rung-1 transient retry + rung-2 self-critique.
    Returns the validated output, or None with ``result.failure_reason`` set."""
    # Every iteration either RETURNS or advances a bounded counter (tool_iters up
    # to max_tool_iterations, critiques up to max_self_critique), so the loop
    # terminates — no arbitrary call cap, no unreachable terminal.
    max_critique = 0 if skip else ladder.max_self_critique
    tool_iters = 0
    critiques = 0
    while True:
        request = runtime.binding.build_request(
            agent_id,
            system=system,
            messages=convo,
            tools=tool_defs,
            max_output_tokens=definition.model_policy.max_output_tokens,
            project_id=str(project_id),
            task_id=str(trigger.task_id) if trigger.task_id else None,
        )
        response = await _llm_call(runtime, agent_id, ladder, skip, request, result)  # rungs 4→1
        result.model_alias = response.served_alias or response.requested_alias or response.model_id
        result.provider = response.provider
        result.input_tokens += response.usage.input_tokens
        result.output_tokens += response.usage.output_tokens

        if response.tool_calls:
            if tool_iters >= runtime.max_tool_iterations:
                # The model keeps requesting tools past its budget — a distinct,
                # observable failure (not invalid output). The ladder escalates.
                logger.warning("runtime.tool_budget_exhausted", agent_id=agent_id)
                result.failure_reason = "tool_budget_exhausted"
                return None
            tool_iters += 1
            await _run_tool_calls(
                runtime,
                agent_id,
                project_id,
                trigger,
                definition.tool_grants,
                caller,
                response,
                convo,
                result,
            )
            continue

        # Step 5 — validate the final structured output.
        try:
            return parse_turn_output(response.text)
        except TurnOutputError as exc:
            if critiques >= max_critique:
                # Never silently accepted (TITAN-AGT-023): the self-critique budget
                # is spent (``critiques`` retries were issued and all failed, or the
                # op skips retries). Give up this turn — the ladder escalates.
                result.output_retries = critiques
                logger.warning(
                    "runtime.output_invalid_exhausted", agent_id=agent_id, retries=critiques
                )
                result.failure_reason = "invalid_output"
                return None
            # Rung 2: re-invoke with the validation failure appended.
            critiques += 1
            result.output_retries = critiques
            _record_rung(result, agent_id, RUNG_SELF_CRITIQUE, attempt=critiques)
            convo.append(Message(role="assistant", content=response.text or ""))
            convo.append(
                Message(
                    role="user",
                    content=(
                        f"Your previous output was invalid: {exc}. "
                        "Return a corrected AgentTurnOutput JSON object only — no prose."
                    ),
                )
            )


async def _escalate(
    runtime: AgentRuntime,
    project_id: uuid.UUID,
    agent_id: str,
    trigger: AgentMessage,
    ladder: LadderConfig,
    result: TurnResult,
) -> TurnResult:
    """Rung 3: rungs 1–2 exhausted. A fail-open agent (docs/critic) logs and lets
    the pipeline proceed; everyone else escalates a ``ProgressReport(blocked)`` to
    its supervisor — or, for the CEO, a structured options memo to the human."""
    reason = result.failure_reason or "invalid_output"
    if ladder.fail_open:
        logger.warning("recovery.fail_open", agent_id=agent_id, reason=reason)
        result.outcome = "failed_open"
        return result

    target = ladder.escalation_target()
    summary = f"{agent_id} could not complete its turn ({reason}) after the recovery ladder."
    options = (
        "Provide client guidance, reassign, or descope."
        if ladder.escalates_to_human
        else "Reassign, decompose the task, or escalate to the human client."
    )
    escalation = build_escalation(target, reason=summary, options=options)
    result.emitted_message_ids = await _emit_messages(
        runtime, project_id, agent_id, trigger, AgentTurnOutput(messages=(escalation,))
    )
    _record_rung(result, agent_id, RUNG_ESCALATE)
    result.escalation_target = target
    result.outcome = "escalated"
    metrics.observe_escalation(f"agent.{agent_id}", target)
    logger.warning(
        "recovery.escalated",
        agent_id=agent_id,
        target=target,
        reason=reason,
        fail_closed=ladder.fail_closed,
    )
    return result


async def _run_tool_calls(
    runtime: AgentRuntime,
    agent_id: str,
    project_id: uuid.UUID,
    trigger: AgentMessage,
    tool_grants: tuple[str, ...],
    caller: ToolCaller,
    response: ChatResponse,
    convo: list[Message],
    result: TurnResult,
) -> None:
    """Enforce grants and execute one round of the model's tool calls, appending
    the assistant tool_use turn and each tool_result to the conversation."""
    assistant_content: list[ContentPart] = [
        ToolUsePart(id=tc.id, name=tc.name, input=tc.arguments) for tc in response.tool_calls
    ]
    if response.text:
        assistant_content.insert(0, TextPart(text=response.text))
    convo.append(Message(role="assistant", content=assistant_content))

    result_parts: list[ContentPart] = []
    for tc in response.tool_calls:
        if not is_granted(tool_grants, tc.name):
            # TITAN-AGT-004: refuse before the Tool Bus, record a security event.
            event = SecurityEvent(
                agent_id=agent_id,
                project_id=project_id,
                task_id=trigger.task_id,
                tool=tc.name,
                arg_keys=len(tc.arguments),
            )
            log_security_event(event)
            result.security_events.append(event)
            result.tool_calls.append(ToolCallRecord(tc.name, "denied"))
            result_parts.append(
                ToolResultPart(
                    tool_use_id=tc.id,
                    content=f"denied: tool '{tc.name}' is not in your tool_grants",
                    is_error=True,
                )
            )
            continue
        call = new_call(
            tc.name,
            tc.arguments,
            project_id=project_id,
            agent_id=f"agent.{agent_id}",
            task_id=trigger.task_id,
        )
        envelope = await runtime.tool_bus.invoke(call, caller, policy=ProjectToolPolicy())
        content, is_error = _tool_result_content(envelope)
        result.tool_calls.append(ToolCallRecord(tc.name, "granted", envelope.status))
        result_parts.append(ToolResultPart(tool_use_id=tc.id, content=content, is_error=is_error))
    convo.append(Message(role="tool", content=result_parts))


def _emitted_id(trigger: AgentMessage, index: int) -> uuid.UUID:
    """The deterministic id of the ``index``-th message a turn for ``trigger``
    emits — stable across reprocesses so re-emission is idempotent."""
    return uuid.uuid5(RUNTIME_MSG_NAMESPACE, f"{trigger.message_id}:{index}")


async def _replayed_outcome(
    runtime: AgentRuntime, emitted: list[uuid.UUID]
) -> tuple[TurnOutcome, str | None]:
    """Recover a replayed turn's outcome from its already-committed emission. A
    single blocked ``ProgressReport`` was a rung-3 escalation (report ``escalated``
    + its target); anything else was a normal completion. The escalation COUNTER is
    not re-incremented on replay, so a fresh-process replay records the turn once
    with the correct label instead of a spurious success."""
    async with runtime.engine.connect() as conn:
        first = await get_message(conn, emitted[0])
    if (
        len(emitted) == 1
        and first is not None
        and first.kind == "progress_report"
        and first.body.get("status") == "blocked"
    ):
        return "escalated", first.to_agent
    return "completed", None


async def _already_emitted(runtime: AgentRuntime, trigger: AgentMessage) -> list[uuid.UUID] | None:
    """The ids a prior attempt of this turn already committed, or None if it has
    not emitted yet. Because emission is atomic (one transaction), a present
    index 0 implies the whole contiguous set is present, so scanning from 0 until
    the first gap recovers exactly the prior attempt's messages."""
    ids: list[uuid.UUID] = []
    async with runtime.engine.connect() as conn:
        index = 0
        while True:
            mid = _emitted_id(trigger, index)
            if not await message_exists(conn, mid):
                break
            ids.append(mid)
            index += 1
    return ids or None


async def _emit_messages(
    runtime: AgentRuntime,
    project_id: uuid.UUID,
    agent_id: str,
    trigger: AgentMessage,
    output: AgentTurnOutput,
) -> list[uuid.UUID]:
    """Persist-then-enqueue every message the turn produced, idempotently.

    Each message gets a deterministic id derived from the triggering message +
    its index (:func:`_emitted_id`); the per-message ``message_exists`` guard plus
    the turn-level :func:`_already_emitted` short-circuit make a reprocessed turn a
    no-op (TITAN-AGT-002/016). All sends share one transaction: either the whole
    turn's messages commit or none do, and the thread/correlation id is inherited
    so the exchange stays one conversation."""
    thread_id = trigger.thread_id or trigger.correlation_id
    emitted: list[uuid.UUID] = []
    async with runtime.engine.begin() as conn:
        for i, om in enumerate(output.messages):
            mid = _emitted_id(trigger, i)
            if await message_exists(conn, mid):
                emitted.append(mid)  # already emitted on a prior attempt — idempotent
                continue
            message = AgentMessage(
                message_id=mid,
                type=om.type,
                project_id=project_id,
                task_id=om.task_id or trigger.task_id,
                thread_id=thread_id,
                correlation_id=trigger.correlation_id,
                from_agent=agent_id,
                to_agent=om.to_agent,
                priority=om.priority,
                requires_ack=om.requires_ack,
                payload=om.payload,
            )
            await send_agent_message(conn, message, actor=agent_id)
            # §7.4 veto capture (TITAN-AGT-018): a Security veto verdict emitted
            # through the normal outbox becomes a pipeline-level merge block, in the
            # SAME transaction as the message so the two commit atomically. A no-op
            # for every non-veto message; idempotent (keyed on the artifact_ref).
            if runtime.vetoing_roles:
                await capture_veto_verdict(
                    conn,
                    project_id,
                    message,
                    vetoing_roles=runtime.vetoing_roles,
                    trigger=trigger,
                )
            emitted.append(mid)
    return emitted


__all__ = [
    "DEFAULT_BACKOFF_BASE_S",
    "DEFAULT_MAX_SELF_CRITIQUE",
    "DEFAULT_MAX_TOOL_ITERATIONS",
    "DEFAULT_MAX_TRANSIENT_RETRIES",
    "RUNTIME_MSG_NAMESPACE",
    "AgentRuntime",
    "FailureReason",
    "ToolCallRecord",
    "TurnOutcome",
    "TurnResult",
    "run_turn",
]
