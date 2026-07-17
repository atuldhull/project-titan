"""LLM cost and token accounting, read side (Vol 4 §6.10, TITAN-FRT-033).

Phase 17 has written every gateway call to ``llm_usage`` since it landed —
provider, model, agent, project, tokens and `cost_usd` — and nothing has ever
read it back out over HTTP. This module is that read side: the aggregation
behind Monitoring's cost tiles and its group-by chart.

Only ``service`` crosses the module boundary (TITAN-BCK-004).
"""
