"""Phase 77 (Vol 9 §6.2, OPS-015): the five Grafana dashboards are provisioned as
valid JSON (dashboards-as-code — no click-ops). Structural only; rendering is a
Compose/Grafana concern the DoD verifies with `docker compose --profile obs up`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

pytestmark: list[Any] = []

# The `<binop> [num] by (...)` antipattern — `by` must sit on an aggregation, never
# after a binary op (this exact shape parse-errored in the LLM-spend cost panel, review #8/#10).
_BINOP_BY = re.compile(r"[-+*/]\s*[\d.]*\s*\bby\b")


def _promql_error(expr: str) -> str | None:
    """A lightweight PromQL sanity check (not a full parser): balanced (){}[] and no
    `<binop> by` shape. Returns an error string or None."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for ch in expr:
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs and (not stack or stack.pop() != pairs[ch]):
            return f"unbalanced '{ch}'"
    if stack:
        return f"unclosed '{stack[-1]}'"
    if _BINOP_BY.search(expr):
        return "`by` follows a binary operator (put `by` on the aggregation)"
    return None


_DASH_DIR = Path(__file__).resolve().parents[3] / "infra" / "grafana" / "dashboards"
_EXPECTED = {
    "titan-health",  # Platform Overview
    "titan-agent-ops",
    "titan-llm-spend",
    "titan-data-stores",
    "titan-slo",
}


def _dashboards() -> list[dict[str, Any]]:
    return [json.loads(p.read_text("utf-8")) for p in _DASH_DIR.glob("*.json")]


def test_all_five_dashboards_present_and_valid() -> None:
    dashboards = _dashboards()
    uids = {d.get("uid") for d in dashboards}
    assert uids >= _EXPECTED, f"missing dashboards: {_EXPECTED - uids}"
    for d in dashboards:
        assert d.get("title"), f"{d.get('uid')} has no title"
        assert d.get("uid"), "dashboard has no uid"
        assert d.get("panels"), f"{d['uid']} has no panels"


def test_every_panel_has_a_query() -> None:
    for d in _dashboards():
        for panel in d["panels"]:
            assert panel.get("title"), f"{d['uid']}: a panel has no title"
            targets = panel.get("targets") or []
            assert targets, f"{d['uid']}/{panel.get('title')}: panel has no targets"
            for t in targets:
                expr = t.get("expr")
                assert expr, f"{d['uid']}/{panel['title']}: a target has no PromQL expr"
                err = _promql_error(expr)
                assert err is None, f"{d['uid']}/{panel['title']}: PromQL {err} in: {expr}"


def test_dashboard_uids_are_unique() -> None:
    uids = [d["uid"] for d in _dashboards()]
    assert len(uids) == len(set(uids)), "duplicate dashboard uids"
