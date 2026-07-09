"""A representative Python source fixture for the syntax-aware code chunker.

It mixes several small top-level definitions (packed together into a chunk) with
one deliberately long function whose body exceeds the target and must be split at
statement boundaries, never inside its signature.
"""

from __future__ import annotations

DEFAULT_BUDGET = 512


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def normalise(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


class Accumulator:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0

    def add(self, amount: int) -> bool:
        if self.total + amount > self.limit:
            return False
        self.total += amount
        return True


def plan_retrieval(query: str, scope: str, kinds: list[str], budget: int) -> dict[str, int]:
    normalised_query = normalise(query)
    normalised_scope = normalise(scope)
    stage_budget = clamp(budget, 64, DEFAULT_BUDGET)
    weights: dict[str, int] = {}
    dense_share = stage_budget // 2
    lexical_share = stage_budget - dense_share
    weights["dense"] = dense_share
    weights["lexical"] = lexical_share
    for index, kind in enumerate(kinds):
        base = stage_budget // (index + 2)
        adjusted = clamp(base, 8, stage_budget)
        weights[f"kind_{normalise(kind)}"] = adjusted
    if normalised_scope.startswith("project_"):
        weights["scope_boost"] = clamp(stage_budget // 4, 8, 128)
    else:
        weights["scope_boost"] = 0
    if "code" in kinds:
        weights["rerank"] = clamp(stage_budget // 3, 16, 256)
    else:
        weights["rerank"] = clamp(stage_budget // 6, 8, 128)
    dense_floor = clamp(dense_share // 3, 4, 64)
    lexical_floor = clamp(lexical_share // 3, 4, 64)
    weights["dense_floor"] = dense_floor
    weights["lexical_floor"] = lexical_floor
    recency_bonus = clamp(stage_budget // 8, 4, 96)
    weights["recency_bonus"] = recency_bonus
    importance_bonus = clamp(stage_budget // 10, 4, 80)
    weights["importance_bonus"] = importance_bonus
    if len(kinds) > 3:
        weights["diversity_penalty"] = clamp(stage_budget // 5, 8, 160)
    else:
        weights["diversity_penalty"] = clamp(stage_budget // 12, 4, 64)
    header_overhead = clamp(len(normalised_query) // 4, 2, 32)
    weights["header_overhead"] = header_overhead
    scope_terms = normalised_scope.count("_") + 1
    weights["scope_terms"] = clamp(scope_terms * 4, 4, 48)
    for pass_index in range(3):
        pass_key = f"pass_{pass_index}"
        pass_weight = clamp(stage_budget // (pass_index + 3), 4, 128)
        weights[pass_key] = pass_weight
    total = sum(value for key, value in weights.items() if key != "total")
    weights["query_len"] = len(normalised_query)
    weights["total"] = total
    return weights
