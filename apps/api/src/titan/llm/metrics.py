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

LLM_LATENCY_SECONDS = Histogram(
    "titan_llm_latency_seconds",
    "LLM call latency by provider/model",
    ["provider", "model"],
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
    """Record one served LLM call across the token/cost/latency collectors."""
    LLM_TOKENS_TOTAL.labels(provider, model, agent, "input").inc(input_tokens)
    LLM_TOKENS_TOTAL.labels(provider, model, agent, "output").inc(output_tokens)
    LLM_TOKENS_TOTAL.labels(provider, model, agent, "cached").inc(cached_tokens)
    LLM_COST_USD_TOTAL.labels(provider, model, agent).inc(cost_usd)
    LLM_LATENCY_SECONDS.labels(provider, model).observe(latency_seconds)


__all__ = [
    "LLM_COST_USD_TOTAL",
    "LLM_FALLBACKS_TOTAL",
    "LLM_LATENCY_SECONDS",
    "LLM_TOKENS_TOTAL",
    "PROJECT_BUDGET_REMAINING_USD",
    "observe_usage",
]
