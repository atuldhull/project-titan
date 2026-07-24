"""Prometheus KPI metrics for the agent runtime (Vol 2 §8 step 7, §2.2 KPIs).

Defining these ``prometheus_client`` collectors registers them on the default
registry, so the existing ``/metrics`` endpoint exports them with no extra wiring.
Labels stay low-cardinality: ``agent`` is bounded by the 16-role roster, and
outcome/status are small enum sets — never a per-instance id, task id, or any
message/prompt content (TITAN-OPS-016).

The §2.2 universal KPIs roll up from these turn-level series in Grafana:
- task success rate  = turns{outcome=completed} / turns;
- escalation rate    = escalations / turns;
- first-pass acceptance = first_pass{first_pass=true} / first_pass (a dedicated
  series, because a *sum* of retries cannot recover the count of zero-retry turns);
- mean tokens per task = the LLM gateway's per-agent token counter / completed turns;
- mean latency per task = titan_agent_turn_latency_seconds.
"""

from prometheus_client import Counter, Gauge, Histogram

AGENT_TURNS_TOTAL = Counter(
    "titan_agent_turns_total",
    "Agent runtime turns, by agent role and outcome (§8 step 7)",
    ["agent", "outcome"],
)

AGENT_TURN_LATENCY_SECONDS = Histogram(
    "titan_agent_turn_latency_seconds",
    "Wall-clock latency of one agent turn, by agent role (§2.2 latency KPI)",
    ["agent"],
)

AGENT_TOOL_CALLS_TOTAL = Counter(
    "titan_agent_tool_calls_total",
    "Model-requested tool calls seen by the runtime, by agent and disposition",
    ["agent", "disposition"],  # granted | denied
)

AGENT_TOOL_GRANT_DENIALS_TOTAL = Counter(
    "titan_agent_tool_grant_denials_total",
    "Ungranted tool calls blocked at the runtime layer (TITAN-AGT-004)",
    ["agent"],
)

AGENT_OUTPUT_RETRIES_TOTAL = Counter(
    "titan_agent_output_retries_total",
    "Self-critique retries triggered by invalid structured output (TITAN-AGT-023)",
    ["agent"],
)

AGENT_MESSAGES_EMITTED_TOTAL = Counter(
    "titan_agent_messages_emitted_total",
    "Structured messages emitted by agent turns, by agent role",
    ["agent"],
)

AGENT_FIRST_PASS_TOTAL = Counter(
    "titan_agent_first_pass_total",
    "Completed turns by whether they were first-pass (zero self-critique retries) "
    "— the §2.2 first-pass-acceptance-rate KPI (rate = first_pass=true / total)",
    ["agent", "first_pass"],
)

# --- recovery ladder (§2.2, TITAN-AGT-005) + universal KPIs (§2.2) ---------

AGENT_RECOVERY_RUNGS_TOTAL = Counter(
    "titan_agent_recovery_rungs_total",
    "Recovery-ladder rungs fired, by agent role and rung (rung1..rung4)",
    ["agent", "rung"],
)

AGENT_ESCALATIONS_TOTAL = Counter(
    "titan_agent_escalations_total",
    "Rung-3 escalations, by agent role and target (a supervisor or 'human') — the "
    "numerator of the §2.2 escalation-rate KPI",
    ["agent", "target"],
)

# --- Orchestrator watchdog (§4.2 TITAN-AGT-009, §10 R6) --------------------

WATCHDOG_RESTARTS_TOTAL = Counter(
    "titan_watchdog_restarts_total",
    "Stalled/dead inbox consumers replaced by the watchdog, by agent role",
    ["agent"],
)

WATCHDOG_ESCALATIONS_TOTAL = Counter(
    "titan_watchdog_escalations_total",
    "Deduplicated watchdog escalations, by cause (unacked | discussion)",
    ["cause"],
)

WATCHDOG_DISCUSSIONS_CLOSED_TOTAL = Counter(
    "titan_watchdog_discussions_closed_total",
    "ArchitectureDiscussion threads auto-closed past their decision deadline (TITAN-AGT-014)",
)

# --- loop detection (§9 TITAN-SEC-083) -------------------------------------

AGENT_LOOPS_DETECTED_TOTAL = Counter(
    "titan_agent_loops_detected_total",
    "Runaway agent loops halted, by agent role and reason (repeat | step_budget)",
    ["agent", "reason"],
)


def observe_loop_detected(agent: str, reason: str) -> None:
    """Record a halted agent loop (TITAN-SEC-083) — the numerator of the loop-rate
    signal and the trigger for the pause-for-review escalation."""
    AGENT_LOOPS_DETECTED_TOTAL.labels(agent, reason).inc()


def observe_watchdog_restart(agent: str) -> None:
    WATCHDOG_RESTARTS_TOTAL.labels(agent).inc()


def observe_watchdog_escalation(cause: str) -> None:
    WATCHDOG_ESCALATIONS_TOTAL.labels(cause).inc()


def observe_discussion_closed() -> None:
    WATCHDOG_DISCUSSIONS_CLOSED_TOTAL.inc()


# The §2.2 universal KPIs are DERIVED in Grafana from the low-cardinality series
# above: task success rate = turns{outcome=completed}/turns; escalation rate =
# escalations/turns; mean tokens per completed task and mean latency roll up from
# the LLM gateway's token counter and titan_agent_turn_latency_seconds. This gauge
# makes each agent's OWN declared kpis (its YAML `kpis` list) visible in Prometheus
# so dashboards can bind to them before the domain phases populate values.
AGENT_KPI_DECLARED = Gauge(
    "titan_agent_kpi_declared",
    "1 for each KPI an agent declares in its AgentDefinition (labelled by agent/kpi)",
    ["agent", "kpi"],
)


def observe_rung(agent: str, rung: str) -> None:
    """Record one recovery-ladder rung firing (one per OTel rung span)."""
    AGENT_RECOVERY_RUNGS_TOTAL.labels(agent, rung).inc()


def observe_escalation(agent: str, target: str) -> None:
    """Record a rung-3 escalation to ``target`` (a supervisor or 'human')."""
    AGENT_ESCALATIONS_TOTAL.labels(agent, target).inc()


def declare_kpis(agent: str, kpis: tuple[str, ...]) -> None:
    """Publish an agent's declared KPI names to Prometheus (value 1) so the series
    exist during a run; the domain phases replace the placeholder with real values."""
    for kpi in kpis:
        AGENT_KPI_DECLARED.labels(agent, kpi).set(1)


def observe_turn(
    *,
    agent: str,
    outcome: str,
    latency_seconds: float,
    granted_tool_calls: int,
    denied_tool_calls: int,
    output_retries: int,
    messages_emitted: int,
) -> None:
    """Record one completed (or failed) agent turn across the KPI collectors."""
    AGENT_TURNS_TOTAL.labels(agent, outcome).inc()
    AGENT_TURN_LATENCY_SECONDS.labels(agent).observe(latency_seconds)
    if outcome == "completed":
        # First-pass acceptance = the agent produced valid output with no
        # self-critique retry. A dedicated series (not derivable from a retry sum).
        AGENT_FIRST_PASS_TOTAL.labels(agent, "true" if output_retries == 0 else "false").inc()
    if granted_tool_calls:
        AGENT_TOOL_CALLS_TOTAL.labels(agent, "granted").inc(granted_tool_calls)
    if denied_tool_calls:
        AGENT_TOOL_CALLS_TOTAL.labels(agent, "denied").inc(denied_tool_calls)
        AGENT_TOOL_GRANT_DENIALS_TOTAL.labels(agent).inc(denied_tool_calls)
    if output_retries:
        AGENT_OUTPUT_RETRIES_TOTAL.labels(agent).inc(output_retries)
    if messages_emitted:
        AGENT_MESSAGES_EMITTED_TOTAL.labels(agent).inc(messages_emitted)


__all__ = [
    "AGENT_ESCALATIONS_TOTAL",
    "AGENT_FIRST_PASS_TOTAL",
    "AGENT_KPI_DECLARED",
    "AGENT_LOOPS_DETECTED_TOTAL",
    "AGENT_MESSAGES_EMITTED_TOTAL",
    "AGENT_OUTPUT_RETRIES_TOTAL",
    "AGENT_RECOVERY_RUNGS_TOTAL",
    "AGENT_TOOL_CALLS_TOTAL",
    "AGENT_TOOL_GRANT_DENIALS_TOTAL",
    "AGENT_TURNS_TOTAL",
    "AGENT_TURN_LATENCY_SECONDS",
    "WATCHDOG_DISCUSSIONS_CLOSED_TOTAL",
    "WATCHDOG_ESCALATIONS_TOTAL",
    "WATCHDOG_RESTARTS_TOTAL",
    "declare_kpis",
    "observe_discussion_closed",
    "observe_escalation",
    "observe_loop_detected",
    "observe_rung",
    "observe_turn",
    "observe_watchdog_escalation",
    "observe_watchdog_restart",
]
