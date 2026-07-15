"""Shared helper: a metered mock LLM gateway for the benchmark tests.

Builds a real :class:`LlmGateway` over an ``httpx.MockTransport`` returning a
canned provider response, so a benchmark run records genuine ``llm_usage`` rows
(the gateway-tracked cost KPI) with no real provider — the same pattern the
budget/accounting tests use.
"""

from pathlib import Path
from typing import Any

import fakeredis.aioredis
import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.llm.config import ConfigLoader
from titan.llm.gateway import LlmGateway
from titan.llm.pricing import PriceBook
from titan.llm.resilience import RetryPolicy

_CONFIG = """
providers:
  anthropic: { api_key: k, base_url: https://api.anthropic.com }
model_aliases:
  primary: { provider: anthropic, model: m, price: { input_per_mtok: 10, output_per_mtok: 10 } }
routing:
  default: primary
"""


def _anthropic_ok() -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }


async def _noop_sleep(_seconds: float) -> None:
    return None


def metered_gateway(engine: AsyncEngine, tmp_path: Path) -> LlmGateway:
    """A gateway that meters ~$0.015/call into ``llm_usage`` against ``engine``."""
    path = tmp_path / "bench_models.yaml"
    path.write_text(_CONFIG, encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_anthropic_ok())

    return LlmGateway(
        ConfigLoader(path),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        pricebook=PriceBook(tmp_path / "no-prices.yaml"),
        engine=engine,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        retry=RetryPolicy(max_retries=1, sleep=_noop_sleep, rand=lambda: 0.0),
    )
