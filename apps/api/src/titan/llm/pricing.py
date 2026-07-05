"""Versioned price table for cost metering (Vol 7 §8.4, TITAN-FND-021).

``config/prices.yaml`` is the authoritative, hot-reloadable price schedule keyed
by provider→model (with a ``"*"`` per-provider wildcard and a global default).
It is separate from models.yaml so prices can be revised without touching
routing — and without a deploy. A per-alias ``price`` in models.yaml still wins
when present (an explicit override), else the book is consulted.
"""

from decimal import Decimal
from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, ConfigDict

from titan.llm.config import Price
from titan.llm.models import Usage

logger = structlog.get_logger("titan.llm.pricing")

_MILLION = Decimal(1_000_000)


def compute_cost(price: Price | None, usage: Usage) -> Decimal:
    """Cost of one call from a price entry; unpriced (or Ollama's zero) ⇒ 0."""
    if price is None:
        return Decimal(0)
    return (
        Decimal(usage.input_tokens) / _MILLION * price.input_per_mtok
        + Decimal(usage.output_tokens) / _MILLION * price.output_per_mtok
    )


class PriceTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    prices: dict[str, dict[str, Price]] = {}
    default: Price = Price()

    def price_for(self, provider: str, model: str) -> Price:
        provider_prices = self.prices.get(provider, {})
        if model in provider_prices:
            return provider_prices[model]
        if "*" in provider_prices:
            return provider_prices["*"]
        return self.default


def parse_prices(raw: str) -> PriceTable:
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("prices.yaml must be a mapping at the top level")
    return PriceTable.model_validate(data)


class PriceBook:
    """Loads and caches prices.yaml, hot-reloading on mtime change. A missing
    file degrades to an all-free table (never a hard failure) so metering never
    takes the gateway down; a warning is logged once per reload attempt."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cached = PriceTable()
        self._mtime: float | None = None

    def table(self) -> PriceTable:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return self._cached
        if mtime != self._mtime:
            try:
                self._cached = parse_prices(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError, yaml.YAMLError) as exc:
                logger.warning("prices.reload_failed", path=str(self._path), error=str(exc))
            self._mtime = mtime
        return self._cached

    def price_for(self, provider: str, model: str) -> Price:
        return self.table().price_for(provider, model)

    def cost(self, provider: str, model: str, usage: Usage) -> Decimal:
        return compute_cost(self.price_for(provider, model), usage)


__all__ = ["PriceBook", "PriceTable", "compute_cost", "parse_prices"]
