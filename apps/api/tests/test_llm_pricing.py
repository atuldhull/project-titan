"""Price-table tests (Phase 17; Vol 7 §8.4)."""

import os
from decimal import Decimal
from pathlib import Path

from titan.llm.config import Price
from titan.llm.models import Usage
from titan.llm.pricing import PriceBook, compute_cost, parse_prices

_PRICES = """
version: 3
prices:
  anthropic:
    claude-sonnet-4-5: { input_per_mtok: 3.00, output_per_mtok: 15.00 }
  ollama:
    "*": { input_per_mtok: 0, output_per_mtok: 0 }
default: { input_per_mtok: 0, output_per_mtok: 0 }
"""


def test_lookup_precedence() -> None:
    table = parse_prices(_PRICES)
    assert table.version == 3
    assert table.price_for("anthropic", "claude-sonnet-4-5").input_per_mtok == Decimal("3.00")
    # provider "*" wildcard (Ollama is free for any model)
    assert table.price_for("ollama", "qwen3-coder:30b").input_per_mtok == Decimal(0)
    # unknown provider → global default
    assert table.price_for("nope", "x").output_per_mtok == Decimal(0)


def test_cost_computation() -> None:
    price = Price(input_per_mtok=Decimal("3.00"), output_per_mtok=Decimal("15.00"))
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert compute_cost(price, usage) == Decimal("18.00")
    assert compute_cost(None, usage) == Decimal(0)


def test_pricebook_hot_reloads(tmp_path: Path) -> None:
    path = tmp_path / "prices.yaml"
    path.write_text(_PRICES, encoding="utf-8")
    book = PriceBook(path)
    assert book.price_for("anthropic", "claude-sonnet-4-5").input_per_mtok == Decimal("3.00")

    path.write_text(
        _PRICES.replace("input_per_mtok: 3.00", "input_per_mtok: 9.99"), encoding="utf-8"
    )
    stat = path.stat()
    os.utime(path, (stat.st_atime + 10, stat.st_mtime + 10))
    assert book.price_for("anthropic", "claude-sonnet-4-5").input_per_mtok == Decimal("9.99")


def test_missing_price_file_degrades_to_free(tmp_path: Path) -> None:
    book = PriceBook(tmp_path / "does-not-exist.yaml")
    assert book.price_for("anthropic", "claude-sonnet-4-5").input_per_mtok == Decimal(0)
    assert book.table().version == 1
