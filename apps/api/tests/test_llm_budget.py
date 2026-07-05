"""Budgets + accounting DoD tests (Phase 17; TITAN-INT-064, TITAN-OPS-021,
TITAN-FND-021). Needs the disposable DB (schema marker) + fakeredis.

Covers: pre-request budget_hold hard-stop, spend accumulation → hold at 100%,
budget-triggered fallback to free local, the fallback AUDIT record, and metered
llm_usage reconciling with provider-reported usage.
"""

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.llm.config import ConfigLoader
from titan.llm.errors import BudgetExceededError
from titan.llm.gateway import LlmGateway
from titan.llm.models import ChatRequest, Message, RequestMetadata
from titan.llm.pricing import PriceBook
from titan.llm.resilience import RetryPolicy

from .conftest import run_async

pytestmark = pytest.mark.schema


async def _noop_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def _clean(migrated_engine: AsyncEngine) -> Any:
    async def clean() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(text("DELETE FROM llm_usage"))
            await conn.execute(text("DELETE FROM budget_holds"))

    run_async(clean)
    yield


def _anthropic_ok(input_tokens: int = 1000, output_tokens: int = 500) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _gateway(
    engine: AsyncEngine,
    tmp_path: Path,
    handler: httpx.MockTransport,
    cfg: str,
) -> LlmGateway:
    path = tmp_path / "models.yaml"
    path.write_text(cfg, encoding="utf-8")
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return LlmGateway(
        ConfigLoader(path),
        httpx.AsyncClient(transport=handler),
        pricebook=PriceBook(tmp_path / "no-prices.yaml"),  # alias prices drive cost
        engine=engine,
        redis=fake,
        retry=RetryPolicy(max_retries=1, sleep=_noop_sleep, rand=lambda: 0.0),
    )


def _meta(project: uuid.UUID, task: uuid.UUID) -> RequestMetadata:
    return RequestMetadata(
        agent_id="agent.backend_engineer", project_id=str(project), task_id=str(task)
    )


def _chat(gateway: LlmGateway, meta: RequestMetadata) -> Any:
    return run_async(
        lambda: gateway.chat(
            ChatRequest(
                messages=[Message(role="user", content="build it")], max_tokens=1000, metadata=meta
            )
        )
    )


# --- pre-request hard stop (TITAN-OPS-021) ---------------------------------

_PAUSE_CONFIG = """
providers:
  anthropic: { api_key: k, base_url: https://api.anthropic.com }
model_aliases:
  primary: { provider: anthropic, model: m, price: { input_per_mtok: 1000, output_per_mtok: 1000 } }
routing:
  default: primary
budgets:
  per_task_usd: 0.01
  action_on_exceeded: pause
"""


def test_budget_hold_blocks_pre_request(
    _clean: Any, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_anthropic_ok())

    gateway = _gateway(migrated_engine, tmp_path, httpx.MockTransport(handler), _PAUSE_CONFIG)
    project, task = uuid.uuid4(), uuid.uuid4()
    meta = _meta(project, task)

    # The estimate (prompt + 1000 max_tokens at $1000/Mtok) already exceeds the
    # $0.01 cap, so the call is rejected BEFORE any provider request.
    with pytest.raises(BudgetExceededError) as exc:
        _chat(gateway, meta)
    assert exc.value.status == 402
    assert calls["n"] == 0  # no provider call exceeded the cap

    # The hold is persisted; a second call is rejected as held.
    async def hold_count() -> int:
        async with migrated_engine.connect() as conn:
            r = await conn.execute(
                text("SELECT count(*) FROM budget_holds WHERE scope='task' AND scope_id=:t"),
                {"t": task},
            )
            return int(r.scalar_one())

    assert run_async(hold_count) == 1
    with pytest.raises(BudgetExceededError):
        _chat(gateway, meta)
    assert calls["n"] == 0


# --- spend accumulation → hold at 100% -------------------------------------

_ACCUM_CONFIG = """
providers:
  anthropic: { api_key: k, base_url: https://api.anthropic.com }
model_aliases:
  primary: { provider: anthropic, model: m, price: { input_per_mtok: 10, output_per_mtok: 10 } }
routing:
  default: primary
budgets:
  per_task_usd: 0.10
  action_on_exceeded: pause
"""


def test_spend_accumulates_then_holds(
    _clean: Any, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    # Each served call costs (1000+500)/1e6 * $10 = $0.015 against a $0.10 cap.
    # The conservative pre-request estimate (assumes the full max_tokens output)
    # stops the run and sets a hold BEFORE actual spend can cross the cap.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_anthropic_ok(1000, 500))

    gateway = _gateway(migrated_engine, tmp_path, httpx.MockTransport(handler), _ACCUM_CONFIG)
    project, task = uuid.uuid4(), uuid.uuid4()
    meta = _meta(project, task)

    served = 0
    for _ in range(20):
        try:
            _chat(gateway, meta)
            served += 1
        except BudgetExceededError:
            break

    async def report() -> tuple[Decimal, int]:
        async with migrated_engine.connect() as conn:
            spend = await conn.execute(
                text("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE task_id=:t"),
                {"t": task},
            )
            holds = await conn.execute(
                text("SELECT count(*) FROM budget_holds WHERE scope='task' AND scope_id=:t"),
                {"t": task},
            )
            return Decimal(spend.scalar_one()), int(holds.scalar_one())

    spent, holds = run_async(report)
    max_call = Decimal("0.015")
    # The cap is enforced to within one maximal request (TITAN-INT-064): the
    # pre-request reservation stops the run, so spend never exceeds cap + 1 call.
    assert spent <= Decimal("0.10") + max_call
    assert holds == 1  # a budget_hold was persisted at the breach
    assert served >= 4


# --- budget-triggered fallback to free local -------------------------------

_FALLBACK_CONFIG = """
providers:
  anthropic: { api_key: k, base_url: https://api.anthropic.com }
  ollama:    { base_url: http://ollama-test:11434 }
model_aliases:
  primary: { provider: anthropic, model: m, price: { input_per_mtok: 1000, output_per_mtok: 1000 } }
  local:   { provider: ollama, model: qwen3-coder:30b }
routing:
  default: primary
  fallbacks:
    primary: [local]
budgets:
  per_task_usd: 0.01
  action_on_exceeded: fallback_then_pause
"""


def test_budget_breach_falls_back_to_local(
    _clean: Any, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/messages"):
            return httpx.Response(500, text="should not be called under budget breach")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "local answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    gateway = _gateway(migrated_engine, tmp_path, httpx.MockTransport(handler), _FALLBACK_CONFIG)
    resp = _chat(gateway, _meta(uuid.uuid4(), uuid.uuid4()))
    # Estimated primary cost breaches the cap → serve free local (no hold).
    assert resp.provider == "ollama"
    assert resp.degraded_model is True
    assert resp.cost_usd == 0


# --- fallback audit record (chaos DoD, audit half) -------------------------


def test_fallback_is_visible_in_audit_record(
    _clean: Any, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    cfg = """
providers:
  anthropic: { api_key: k, base_url: https://api.anthropic.com }
  ollama:    { base_url: http://ollama-test:11434 }
model_aliases:
  primary: { provider: anthropic, model: m, context_window: 200000 }
  local:   { provider: ollama, model: qwen3-coder:30b, context_window: 32768 }
routing:
  default: primary
  fallbacks:
    primary: [local]
"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/messages"):
            return httpx.Response(503, text="blackholed")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "fallback"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    gateway = _gateway(migrated_engine, tmp_path, httpx.MockTransport(handler), cfg)
    project, task = uuid.uuid4(), uuid.uuid4()
    resp = _chat(gateway, _meta(project, task))
    assert resp.degraded_model is True

    async def audit_row() -> Any:
        async with migrated_engine.connect() as conn:
            r = await conn.execute(
                text(
                    "SELECT provider, requested_alias, served_alias, degraded_model "
                    "FROM llm_usage WHERE task_id=:t"
                ),
                {"t": task},
            )
            return r.first()

    row = run_async(audit_row)
    assert row is not None
    assert row.provider == "ollama"
    assert row.requested_alias == "primary"
    assert row.served_alias == "local"
    assert row.degraded_model is True


# --- metering reconciliation (TITAN-FND-021, within 2%) --------------------

_METER_CONFIG = """
providers:
  anthropic: { api_key: k, base_url: https://api.anthropic.com }
model_aliases:
  primary:
    provider: anthropic
    model: m
    price: { input_per_mtok: 3.00, output_per_mtok: 15.00 }
routing:
  default: primary
"""


def test_metered_usage_reconciles_with_provider_reported(
    _clean: Any, migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    # Provider-reported usage embedded in the wire fixture.
    reported_in, reported_out = 1200, 800

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_anthropic_ok(reported_in, reported_out))

    gateway = _gateway(migrated_engine, tmp_path, httpx.MockTransport(handler), _METER_CONFIG)
    project, task = uuid.uuid4(), uuid.uuid4()
    meta = _meta(project, task)
    n = 5
    for _ in range(n):
        _chat(gateway, meta)

    async def totals() -> tuple[int, int, Decimal]:
        async with migrated_engine.connect() as conn:
            r = await conn.execute(
                text(
                    "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                    "COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE task_id=:t"
                ),
                {"t": task},
            )
            row = r.one()
            return int(row[0]), int(row[1]), Decimal(row[2])

    tin, tout, metered_cost = run_async(totals)
    # Token counts reconcile EXACTLY with provider-reported usage.
    assert tin == reported_in * n
    assert tout == reported_out * n
    # Cost reconciles within 2% of an independent computation from the schedule.
    expected = n * (
        Decimal(reported_in) / 1_000_000 * Decimal("3.00")
        + Decimal(reported_out) / 1_000_000 * Decimal("15.00")
    )
    assert abs(metered_cost - expected) <= expected * Decimal("0.02")
