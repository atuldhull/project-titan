"""Retrieval evaluation harness (Vol 5 §6.4, TITAN-BRN-011) — private to `memory`.

The quality gate: a VERSIONED golden dataset of query→relevant-record pairs is
seeded through the real ingest path, retrieval runs over it, and recall@10 +
nDCG@10 are compared against the checked-in baseline — any regression > 5 %
fails the build. The same primitives serve the TITAN-BRN-004 rebuild gate
(``overlap_recall`` ≥ 0.99 between pre-delete and post-rebuild top-10 lists).

This module owns only the pure parts — dataset schema, metrics, baseline
comparison. Driving retrieval (real or hermetic) belongs to the callers: the
CI gate (``tests/test_memory_eval_gate.py``), the performance harness
(``scripts/bench_retrieval.py``), and the live rebuild gate.
"""

import json
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# TITAN-BRN-011: both metrics are @10; regressions beyond 5 % fail the build.
EVAL_K = 10
MAX_REGRESSION = 0.05
# TITAN-BRN-004 acceptance: post-rebuild results ≥ 0.99 recall vs pre-delete.
REBUILD_MIN_RECALL = 0.99

_KINDS = ("episodic", "semantic", "procedural")


class EvalRecord(BaseModel):
    """One golden corpus document. ``scope`` is symbolic — the harness maps
    ``project`` to a concrete ``project:<uuid>`` it creates at seed time."""

    model_config = ConfigDict(extra="forbid")

    marker: str = Field(min_length=4, max_length=80)
    kind: Literal["episodic", "semantic", "procedural"]
    scope: Literal["org", "project"] = "org"
    title: str = Field(min_length=1, max_length=300)
    body_md: str = Field(min_length=1)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


class EvalPair(BaseModel):
    """One golden query. ``leg`` labels WHY the pair exists (which retrieval
    path it stresses) — diagnostics only, never weighting."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    expected_marker: str
    scope: Literal["org", "project"] = "org"
    kinds: list[str] = Field(default_factory=list)
    leg: Literal["lexical", "hybrid", "probe"] = "hybrid"


class EvalDataset(BaseModel):
    """The versioned golden dataset (TITAN-BRN-011: ≥ 200 pairs, all three
    kinds, both scopes, exact-identifier queries)."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    description: str = ""
    records: list[EvalRecord] = Field(min_length=1)
    pairs: list[EvalPair] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_referential_integrity(self) -> "EvalDataset":
        markers = [record.marker for record in self.records]
        duplicates = {m for m in markers if markers.count(m) > 1}
        if duplicates:
            raise ValueError(f"duplicate record markers: {sorted(duplicates)}")
        by_marker = {record.marker: record for record in self.records}
        for pair in self.pairs:
            record = by_marker.get(pair.expected_marker)
            if record is None:
                raise ValueError(f"pair references unknown marker {pair.expected_marker!r}")
            if pair.scope == "org" and record.scope == "project":
                # TITAN-BRN-003: an org-scoped query can NEVER see a project
                # record — such a pair would bake an isolation violation into
                # the gate's definition of success.
                raise ValueError(
                    f"pair {pair.query!r} expects project record "
                    f"{pair.expected_marker!r} from org scope"
                )
            unknown = [kind for kind in pair.kinds if kind not in _KINDS]
            if unknown:
                raise ValueError(f"pair {pair.query!r} has unknown kinds {unknown}")
            if pair.kinds and record.kind not in pair.kinds:
                raise ValueError(
                    f"pair {pair.query!r} filters kinds={pair.kinds} but expects "
                    f"a {record.kind} record — it could never succeed"
                )
        return self

    def coverage_summary(self) -> dict[str, int]:
        """Sanity counts the gate asserts on (the TITAN-BRN-011 shape)."""
        return {
            "pairs": len(self.pairs),
            "records": len(self.records),
            **{f"kind_{kind}": sum(1 for r in self.records if r.kind == kind) for kind in _KINDS},
            "scope_project_records": sum(1 for r in self.records if r.scope == "project"),
            "scope_project_pairs": sum(1 for p in self.pairs if p.scope == "project"),
            "lexical_pairs": sum(1 for p in self.pairs if p.leg in ("lexical", "probe")),
        }


def load_dataset(path: Path) -> EvalDataset:
    return EvalDataset.model_validate(json.loads(path.read_text(encoding="utf-8")))


# --- metrics -------------------------------------------------------------------


def recall_at_k(ranked: Sequence[uuid.UUID], relevant: uuid.UUID, k: int = EVAL_K) -> float:
    """Binary recall@k for a single-relevant-record query."""
    return 1.0 if relevant in list(ranked)[:k] else 0.0


def ndcg_at_k(ranked: Sequence[uuid.UUID], relevant: uuid.UUID, k: int = EVAL_K) -> float:
    """nDCG@k with binary relevance and one relevant record: the ideal DCG is
    1 (relevant at rank 1), so nDCG = 1/log2(rank+1), 0 when out of the top k."""
    top = list(ranked)[:k]
    if relevant not in top:
        return 0.0
    rank = top.index(relevant) + 1  # 1-based
    return 1.0 / math.log2(rank + 1.0)


class EvalReport(BaseModel):
    """One evaluation run's metrics (per reranker mode)."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    pairs: int
    recall_at_10: float
    ndcg_at_10: float
    misses: list[str] = Field(default_factory=list)


RunQuery = Callable[[EvalPair], Awaitable[Sequence[uuid.UUID]]]


async def evaluate(
    dataset: EvalDataset,
    ids_by_marker: Mapping[str, uuid.UUID],
    run_query: RunQuery,
    *,
    mode: str,
) -> EvalReport:
    """Run every golden pair through ``run_query`` (→ ranked record ids) and
    aggregate recall@10 + nDCG@10 (TITAN-BRN-011's two reported metrics)."""
    recalls: list[float] = []
    ndcgs: list[float] = []
    misses: list[str] = []
    for pair in dataset.pairs:
        expected = ids_by_marker[pair.expected_marker]
        ranked = list(await run_query(pair))
        recall = recall_at_k(ranked, expected)
        recalls.append(recall)
        ndcgs.append(ndcg_at_k(ranked, expected))
        if recall == 0.0:
            misses.append(f"[{pair.leg}/{pair.scope}] {pair.query}")
    return EvalReport(
        mode=mode,
        pairs=len(dataset.pairs),
        recall_at_10=sum(recalls) / len(recalls),
        ndcg_at_10=sum(ndcgs) / len(ndcgs),
        misses=misses,
    )


# --- the baseline gate -----------------------------------------------------------


class ModeMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recall_at_10: float = Field(ge=0.0, le=1.0)
    ndcg_at_10: float = Field(ge=0.0, le=1.0)


class EvalBaseline(BaseModel):
    """The stored baseline the CI gate compares against — regenerate it
    (consciously, in a reviewed commit) whenever the dataset version bumps or
    retrieval genuinely improves."""

    model_config = ConfigDict(extra="forbid")

    dataset_version: int
    modes: dict[str, ModeMetrics]


def load_baseline(path: Path) -> EvalBaseline:
    return EvalBaseline.model_validate(json.loads(path.read_text(encoding="utf-8")))


def check_baseline(
    report: EvalReport,
    baseline: ModeMetrics,
    *,
    max_regression: float = MAX_REGRESSION,
) -> list[str]:
    """TITAN-BRN-011's build gate: every metric must stay within
    ``max_regression`` (relative) of the stored baseline. Returns human-readable
    failures; empty means the gate passes."""
    failures: list[str] = []
    for name, current, base in (
        ("recall@10", report.recall_at_10, baseline.recall_at_10),
        ("ndcg@10", report.ndcg_at_10, baseline.ndcg_at_10),
    ):
        floor = base * (1.0 - max_regression)
        if current < floor - 1e-9:
            failures.append(
                f"mode={report.mode} {name}={current:.4f} regressed more than "
                f"{max_regression:.0%} below baseline {base:.4f} (floor {floor:.4f})"
            )
    return failures


def overlap_recall(
    before: Sequence[Sequence[uuid.UUID]], after: Sequence[Sequence[uuid.UUID]]
) -> float:
    """TITAN-BRN-004's rebuild metric: mean fraction of each pre-delete top-k
    list recovered by the post-rebuild top-k list. 1.0 = identical results.

    Caveat for harness authors: the corpus must be textually DISTINCT.
    Near-duplicate documents score near-identical similarities, and two HNSW
    index builds order near-ties differently — the metric then measures tie
    churn in the top-k tail, not rebuild fidelity (observed live: relevant
    recall identical before/after while overlap sat at 0.48 on a corpus of
    templated near-clones)."""
    if len(before) != len(after):
        raise ValueError(f"result-list count mismatch: {len(before)} vs {len(after)}")
    fractions: list[float] = []
    for old, new in zip(before, after, strict=True):
        if not old:
            continue  # a query with no pre-delete results constrains nothing
        fractions.append(len(set(old) & set(new)) / len(old))
    return sum(fractions) / len(fractions) if fractions else 1.0


__all__ = [
    "EVAL_K",
    "MAX_REGRESSION",
    "REBUILD_MIN_RECALL",
    "EvalBaseline",
    "EvalDataset",
    "EvalPair",
    "EvalRecord",
    "EvalReport",
    "ModeMetrics",
    "check_baseline",
    "evaluate",
    "load_baseline",
    "load_dataset",
    "ndcg_at_k",
    "overlap_recall",
    "recall_at_k",
]
