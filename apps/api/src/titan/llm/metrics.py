"""Prometheus metrics for the LLM gateway (Vol 7 §8.4, Vol 9 §6).

Defining these ``prometheus_client`` collectors registers them on the default
registry, so the existing ``/metrics`` endpoint exports them with no extra wiring.
Labels are kept low-cardinality: provider/model/agent are bounded sets; latency
drops the agent label; the budget gauge is per-project.
"""

from prometheus_client import Counter, Gauge, Histogram

LLM_TOKENS_TOTAL = Counter(
    "titan_llm_tokens_total",
    "LLM tokens consumed, by direction (TITAN-FND-021)",
    ["provider", "model", "agent", "direction"],
)

LLM_COST_USD_TOTAL = Counter(
    "titan_llm_cost_usd_total",
    "LLM cost in USD (TITAN-FND-021)",
    ["provider", "model", "agent"],
)

# Agent-role-level token counter (Vol 9 §6.1 titan_agent_tokens_total): the gateway
# per-provider/model counter above rolled up to the ROLE, so the Agent Operations
# dashboard needs no per-provider fan-out. `agent` is role-bucketed upstream (no
# per-task cardinality).
AGENT_TOKENS_TOTAL = Counter(
    "titan_agent_tokens_total",
    "Prompt/completion tokens per agent role (Vol 9 §6.1)",
    ["agent", "direction"],
)

# Vol 9 §6.1 spec name (was titan_llm_latency_seconds). Gateway-observed LLM latency.
LLM_REQUEST_DURATION_SECONDS = Histogram(
    "titan_llm_request_duration_seconds",
    "Gateway-observed LLM request latency by provider/model (Vol 9 §6.1)",
    ["provider", "model"],
)

# Vol 9 §6.1 titan_llm_errors_total{kind}: provider errors by class, so the LLM Spend
# dashboard + LLMProviderDegraded alert can compute a per-provider error rate.
LLM_ERRORS_TOTAL = Counter(
    "titan_llm_errors_total",
    "LLM provider errors by kind (rate_limit/timeout/refusal/server_error) (Vol 9 §6.1)",
    ["provider", "kind"],
)

LLM_FALLBACKS_TOTAL = Counter(
    "titan_llm_fallbacks_total",
    "Fallback substitutions, by requested→served alias (§8.3)",
    ["requested", "served"],
)

PROJECT_BUDGET_REMAINING_USD = Gauge(
    "titan_project_budget_remaining_usd",
    "Remaining per-project LLM budget in USD (Vol 9 §10)",
    ["project"],
)


def observe_usage(
    *,
    provider: str,
    model: str,
    agent: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost_usd: float,
    latency_seconds: float,
) -> None:
    """Record one served LLM call across the token/cost/latency collectors. This is
    the SINGLE gateway choke point (OPS-014): the same call also writes the llm_usage
    Postgres ledger, so Σ titan_llm_cost_usd_total reconciles with the ledger."""
    LLM_TOKENS_TOTAL.labels(provider, model, agent, "input").inc(input_tokens)
    LLM_TOKENS_TOTAL.labels(provider, model, agent, "output").inc(output_tokens)
    LLM_TOKENS_TOTAL.labels(provider, model, agent, "cached").inc(cached_tokens)
    AGENT_TOKENS_TOTAL.labels(agent, "input").inc(input_tokens)
    AGENT_TOKENS_TOTAL.labels(agent, "output").inc(output_tokens)
    LLM_COST_USD_TOTAL.labels(provider, model, agent).inc(cost_usd)
    LLM_REQUEST_DURATION_SECONDS.labels(provider, model).observe(latency_seconds)


def observe_error(*, provider: str, kind: str) -> None:
    """Record one provider error at the gateway choke point (OPS-014)."""
    LLM_ERRORS_TOTAL.labels(provider, kind).inc()


__all__ = [
    "AGENT_TOKENS_TOTAL",
    "LLM_COST_USD_TOTAL",
    "LLM_ERRORS_TOTAL",
    "LLM_FALLBACKS_TOTAL",
    "LLM_REQUEST_DURATION_SECONDS",
    "LLM_TOKENS_TOTAL",
    "PROJECT_BUDGET_REMAINING_USD",
    "observe_error",
    "observe_usage",
]
