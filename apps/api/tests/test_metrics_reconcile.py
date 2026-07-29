"""Phase 77 (Vol 9 §6.1, OPS-014): custom-metrics existence + the gateway
cost-counter ↔ Postgres-ledger reconciliation-within-1% acceptance.

Needs the disposable DB (schema marker) + fakeredis.
"""

import uuid
from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.llm.config import ConfigLoader
from titan.llm.gateway import LlmGateway
from titan.llm.models import ChatRequest, Message, RequestMetadata
from titan.llm.pricing import PriceBook
from titan.llm.resilience import RetryPolicy

from .conftest import run_async

pytestmark = pytest.mark.schema

_TEN_SPEC_METRICS = [
    "titan_agent_tasks_total",
    "titan_agent_task_duration_seconds",
    "titan_agent_tokens_total",
    "titan_llm_cost_usd_total",
    "titan_llm_request_duration_seconds",
    "titan_llm_errors_total",
    "titan_queue_depth",
    "titan_queue_oldest_pending_seconds",
    "titan_project_budget_remaining_usd",
    "titan_agent_review_rejections_total",
]

_CONFIG = """
providers:
  anthropic: { api_key: k, base_url: https://api.anthropic.com }
model_aliases:
  primary: { provider: anthropic, model: m, price: { input_per_mtok: 1000, output_per_mtok: 1000 } }
routing:
  default: primary
"""


def test_all_ten_spec_metrics_are_registered() -> None:
    # Ensure the three metric modules are imported so their families register.
    import titan.llm.metrics  # noqa: F401
    import titan.observability.metrics  # noqa: F401
    import titan.runtime.metrics  # noqa: F401

    # Match the EXACT family name (review #5 — a loose startswith let a misnamed
    # metric like titan_queue_depth_v2 or titan_agent_task_duration pass). A Counter's
    # family drops the `_total` suffix; gauges/histograms keep their full name.
    families = {m.name for m in REGISTRY.collect()}
    for spec in _TEN_SPEC_METRICS:
        family = spec[:-6] if spec.endswith("_total") else spec
        assert family in families, f"{spec} (family '{family}') is not registered exactly"


def _cost_counter_total() -> float:
    """Σ titan_llm_cost_usd_total across all label sets (the Prometheus side)."""
    total = 0.0
    for metric in REGISTRY.collect():
        for s in metric.samples:
            if s.name == "titan_llm_cost_usd_total":
                total += s.value
    return total


async def _ledger_cost(engine: AsyncEngine) -> float:
    async with engine.connect() as conn:
        row = await conn.execute(text("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage"))
        return float(row.scalar_one())


def _anthropic_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        },
    )


def test_cost_counter_reconciles_with_ledger_within_1pct(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    async def clean() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(text("DELETE FROM llm_usage"))

    run_async(clean)
    path = tmp_path / "models.yaml"
    path.write_text(_CONFIG, encoding="utf-8")
    gateway = LlmGateway(
        ConfigLoader(path),
        httpx.AsyncClient(transport=httpx.MockTransport(_anthropic_ok)),
        pricebook=PriceBook(tmp_path / "no-prices.yaml"),
        engine=migrated_engine,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        retry=RetryPolicy(max_retries=1, sleep=lambda _s: _noop(), rand=lambda: 0.0),
    )
    before = _cost_counter_total()  # the counter is global — measure the delta

    project, task = uuid.uuid4(), uuid.uuid4()
    for _ in range(5):
        run_async(
            lambda: gateway.chat(
                ChatRequest(
                    messages=[Message(role="user", content="build it")],
                    max_tokens=1000,
                    metadata=RequestMetadata(
                        agent_id="agent.backend_engineer",
                        project_id=str(project),
                        task_id=str(task),
                    ),
                )
            )
        )

    counter_delta = _cost_counter_total() - before
    ledger = run_async(lambda: _ledger_cost(migrated_engine))
    # An INDEPENDENT expected cost from the mock's tokens × the config price, so the
    # reconciliation is NOT tautological (review #3/#4 — both sinks derive from the same
    # cost variable, so a pricing drift shifts both identically; anchoring to an
    # independently-computed value catches that). 5 calls × (1000 in + 500 out @ 1000/mtok).
    expected = 5 * (1000 / 1e6 * 1000 + 500 / 1e6 * 1000)  # = 7.5
    assert ledger > 0, "the ledger recorded no cost"
    # OPS-014 acceptance: BOTH the gateway counter and the Postgres ledger reconcile
    # with the independent expected within 1%, and with each other.
    assert abs(counter_delta - expected) / expected < 0.01, f"counter {counter_delta} vs {expected}"
    assert abs(ledger - expected) / expected < 0.01, f"ledger {ledger} vs {expected}"
    assert abs(counter_delta - ledger) / max(ledger, 1e-9) < 0.01


async def _noop() -> None:
    return None
