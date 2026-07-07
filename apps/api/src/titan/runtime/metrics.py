"""Prometheus KPI metrics for the agent runtime (Vol 2 §8 step 7, §2.2 KPIs).

Defining these ``prometheus_client`` collectors registers them on the default
registry, so the existing ``/metrics`` endpoint exports them with no extra wiring.
Labels stay low-cardinality: ``agent`` is bounded by the 16-role roster, and
outcome/status are small enum sets — never a per-instance id, task id, or any
message/prompt content (TITAN-OPS-016).

The §2.2 universal KPIs (task success rate, first-pass acceptance, mean tokens
per task, latency, escalation rate) roll up from these turn-level series in
Grafana; per-turn token/cost is already metered by the LLM gateway.
"""

from prometheus_client import Counter, Histogram

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
    "AGENT_MESSAGES_EMITTED_TOTAL",
    "AGENT_OUTPUT_RETRIES_TOTAL",
    "AGENT_TOOL_CALLS_TOTAL",
    "AGENT_TOOL_GRANT_DENIALS_TOTAL",
    "AGENT_TURNS_TOTAL",
    "AGENT_TURN_LATENCY_SECONDS",
    "observe_turn",
]
