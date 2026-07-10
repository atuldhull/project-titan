"""The Phase-37 retrieval quality gate (TITAN-BRN-011, TITAN-BRN-004, §8.2).

Seeds the versioned golden dataset (224 pairs) through the real ingest path,
runs retrieval in all three reranker modes, and fails the build if recall@10
or nDCG@10 regresses more than 5 % against the stored baseline. Also proves
gate discrimination (a deliberately corrupted fusion weight FAILS it — the
TITAN-BRN-011 acceptance), exercises packets at tight budgets (§8.2), and runs
the hermetic TITAN-BRN-004 rebuild gate: index gone → `rebuild_index` →
recall@10 ≥ 0.99 vs pre-delete results.

CI runs this module standalone as the `retrieval-eval` job (`make
eval-retrieval`); refresh the baseline consciously with `make eval-baseline`.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from tests.memory_fakes import FakeQdrantServer, HashingEmbeddings
from tests.memory_harness import Pipeline, create_project, quiesce_outbox
from titan.memory import evaluation, retrieval
from titan.memory.qdrant import QdrantIndex
from titan.memory.rebuild import rebuild_index
from titan.memory.retrieval import RetrievalQuery, retrieve

pytestmark = pytest.mark.schema

GOLDEN_DIR = Path(__file__).parent / "golden"
DATASET_PATH = GOLDEN_DIR / "memory_eval_v1.json"
BASELINE_PATH = GOLDEN_DIR / "memory_eval_baseline.json"
MODES = ("on", "llm", "off")


def _report(line: str) -> None:
    """Metric reporting the TITAN-BRN-011 CI job surfaces: stdout always, plus
    the GitHub Actions step summary when available."""
    print(line)  # noqa: T201 — the CI job's visible metrics output
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(line + "\n\n")


class _Harness:
    """One seeded golden corpus: dataset records (org + a fresh project)
    ingested through the real path, with a per-pair retrieval driver."""

    def __init__(self, api: dict[str, Any], mode: str) -> None:
        self.dataset = evaluation.load_dataset(DATASET_PATH)
        self.engine: AsyncEngine = api["engine"]
        self.project_id = create_project(api, f"eval-{mode}")
        self.pipeline = Pipeline(self.engine, mode=mode, embeddings=HashingEmbeddings())
        self.ids: dict[str, uuid.UUID] = {}

    def scope_of(self, symbolic: str) -> str:
        return "org" if symbolic == "org" else f"project:{self.project_id}"

    async def seed(self) -> None:
        """Seed the golden corpus, isolated from earlier eval runs on the
        shared DB: bodies carry a per-run nonce (identical text across runs
        would mean identical vectors, and cosine TIES resolve by index
        insertion order — nondeterministic ranks), and every stale copy of an
        EVL record is flipped to superseded so the status='active' filters
        keep old duplicates out of the lexical top-40."""
        await quiesce_outbox(self.engine)
        nonce = uuid.uuid4().hex[:12]
        self.ids = await self.pipeline.seed(
            [
                {
                    "marker": record.marker,
                    "kind": record.kind,
                    "scope": self.scope_of(record.scope),
                    "title": record.title,
                    "body_md": f"{record.body_md} Run tag {nonce}.",
                    "importance": record.importance,
                }
                for record in self.dataset.records
            ]
        )
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE brain.memory_records SET status = 'superseded' "
                    "WHERE title LIKE :pattern ESCAPE '\\' "
                    "AND NOT (id = ANY(CAST(:ids AS uuid[])))"
                ),
                {
                    "pattern": r"%(EVL\_%",
                    "ids": [str(record_id) for record_id in self.ids.values()],
                },
            )

    async def run_query(self, pair: evaluation.EvalPair) -> list[uuid.UUID]:
        ranked = await retrieve(
            self.engine,
            self.pipeline.qdrant,
            self.pipeline.embeddings,
            self.pipeline.reranker,
            RetrievalQuery(
                query=pair.query, scope=self.scope_of(pair.scope), kinds=tuple(pair.kinds)
            ),
        )
        return [chunk.record_id for chunk in ranked]

    async def evaluate(self, mode: str) -> evaluation.EvalReport:
        return await evaluation.evaluate(self.dataset, self.ids, self.run_query, mode=mode)


# --- the dataset itself is part of the contract -----------------------------------


def test_golden_dataset_shape() -> None:
    """TITAN-BRN-011: ≥ 200 pairs, all three kinds, both scopes, and a real
    population of exact-identifier (lexical-leg) queries."""
    summary = evaluation.load_dataset(DATASET_PATH).coverage_summary()
    assert summary["pairs"] >= 200
    assert summary["kind_episodic"] >= 20
    assert summary["kind_semantic"] >= 20
    assert summary["kind_procedural"] >= 20
    assert summary["scope_project_records"] >= 20
    assert summary["scope_project_pairs"] >= 40
    assert summary["lexical_pairs"] >= 60


# --- the quality gate, in every reranker mode (§15 R4) -----------------------------


@pytest.mark.parametrize("mode", MODES)
def test_eval_gate_against_baseline(api: dict[str, Any], mode: str) -> None:
    harness = _Harness(api, mode)

    async def scenario() -> evaluation.EvalReport:
        await harness.seed()
        return await harness.evaluate(mode)

    report = run_async(scenario)
    _report(
        f"MEMORY-EVAL mode={mode} pairs={report.pairs} "
        f"recall@10={report.recall_at_10:.4f} ndcg@10={report.ndcg_at_10:.4f}"
    )
    if report.misses:
        print(f"  misses ({len(report.misses)}): {report.misses[:10]}")  # noqa: T201

    if os.environ.get("TITAN_EVAL_WRITE_BASELINE"):
        _update_baseline(mode, report, harness.dataset.version)
        return
    # Whatever the baseline says, the golden set must broadly work — a garbage
    # baseline must not be able to bless a broken retriever.
    assert report.recall_at_10 >= 0.9, report.misses
    baseline = evaluation.load_baseline(BASELINE_PATH)
    assert baseline.dataset_version == harness.dataset.version, (
        "baseline was computed for a different dataset version — regenerate it "
        "with `make eval-baseline`"
    )
    failures = evaluation.check_baseline(report, baseline.modes[mode])
    assert not failures, failures


def _update_baseline(mode: str, report: evaluation.EvalReport, dataset_version: int) -> None:
    current: dict[str, Any] = {"dataset_version": dataset_version, "modes": {}}
    if BASELINE_PATH.exists():
        current = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    current["dataset_version"] = dataset_version
    current.setdefault("modes", {})[mode] = {
        "recall_at_10": round(report.recall_at_10, 4),
        "ndcg_at_10": round(report.ndcg_at_10, 4),
    }
    BASELINE_PATH.write_text(json.dumps(current, indent=1) + "\n", encoding="utf-8")
    _report(f"MEMORY-EVAL baseline updated for mode={mode}")


# --- DoD: a corrupted fusion weight FAILS the gate ---------------------------------


@pytest.mark.parametrize(
    ("constant", "broken_value"),
    [
        ("RRF_K", -10_000),  # stage 3: the RRF fusion constant Σ 1/(K + rank)
        ("WEIGHT_RERANK", 0.0),  # stage 5: relevance stripped from the final key
    ],
)
def test_broken_fusion_weight_fails_the_gate(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, constant: str, broken_value: float
) -> None:
    """TITAN-BRN-011 acceptance: the gate must DETECT a broken fusion, not just
    pass a healthy build. Corrupt one weight, re-run the same eval, and require
    check_baseline to report a >5 % regression."""
    harness = _Harness(api, "on")

    async def scenario() -> tuple[evaluation.EvalReport, evaluation.EvalReport]:
        await harness.seed()
        clean = await harness.evaluate("on")
        monkeypatch.setattr(retrieval, constant, broken_value)
        if constant == "WEIGHT_RERANK":
            # Keep the key normalised so ordering is decided purely by
            # importance/decay — relevance no longer participates at all.
            monkeypatch.setattr(retrieval, "WEIGHT_DECAY", 0.0)
            monkeypatch.setattr(retrieval, "WEIGHT_IMPORTANCE", 1.0)
        corrupted = await harness.evaluate("on")
        monkeypatch.undo()
        return clean, corrupted

    clean, corrupted = run_async(scenario)
    baseline = evaluation.load_baseline(BASELINE_PATH)
    assert not evaluation.check_baseline(clean, baseline.modes["on"]), (
        "clean run must pass the gate for this test to prove discrimination"
    )
    failures = evaluation.check_baseline(corrupted, baseline.modes["on"])
    assert failures, (
        f"corrupting {constant} did not fail the gate: "
        f"clean recall@10={clean.recall_at_10:.4f} -> "
        f"corrupted {corrupted.recall_at_10:.4f} / ndcg {corrupted.ndcg_at_10:.4f}"
    )
    _report(
        f"MEMORY-EVAL negative-control {constant}={broken_value}: recall@10 "
        f"{clean.recall_at_10:.4f} -> {corrupted.recall_at_10:.4f} (gate FAILED as required)"
    )


# --- §8.2: packets under tight budgets ---------------------------------------------


def test_packets_hold_contract_at_tight_budgets(api: dict[str, Any]) -> None:
    """The eval must exercise packet assembly at tight budgets (§8.2): the
    budget is respected, identity_context survives (rule c), every included
    chunk keeps its citation (rule a), and the tight packet is a strict prefix
    of the roomy packet's ranking — eviction, never reordering."""
    harness = _Harness(api, "on")
    lexical_pairs = [p for p in evaluation.load_dataset(DATASET_PATH).pairs if p.leg == "lexical"]

    async def scenario() -> tuple[int, int, list[str]]:
        await harness.seed()
        hits = 0
        violations: list[str] = []
        for pair in lexical_pairs:
            scope = harness.scope_of(pair.scope)
            tight = await harness.pipeline.packet(
                query=pair.query, scope=scope, kinds=pair.kinds, budget_tokens=256
            )
            roomy = await harness.pipeline.packet(
                query=pair.query, scope=scope, kinds=pair.kinds, budget_tokens=8000
            )
            if tight.token_cost > 256:
                violations.append(f"{pair.query}: token_cost {tight.token_cost} > 256")
            if not tight.sections.identity_context:
                violations.append(f"{pair.query}: identity_context evicted")
            included = (
                len(tight.sections.procedural)
                + len(tight.sections.semantic)
                + len(tight.sections.episodic)
            )
            if len(tight.citations) != included:
                violations.append(f"{pair.query}: citations {len(tight.citations)} != {included}")
            tight_ids = [c.chunk_id for c in tight.citations]
            roomy_ids = [c.chunk_id for c in roomy.citations]
            if roomy_ids[: len(tight_ids)] != tight_ids:
                violations.append(f"{pair.query}: tight packet is not a prefix of the roomy one")
            if harness.ids[pair.expected_marker] in {c.record_id for c in tight.citations}:
                hits += 1
        return hits, len(lexical_pairs), violations

    hits, total, violations = run_async(scenario)
    assert violations == []
    # Even at 256 tokens the top-ranked (relevant) record must almost always fit.
    assert hits / total >= 0.9, f"tight-budget recall {hits}/{total}"


# --- TITAN-BRN-004: index gone -> rebuild -> recall@10 >= 0.99 ---------------------


def test_rebuild_restores_golden_results(api: dict[str, Any]) -> None:
    """The hermetic rebuild gate: complete index, capture golden top-10s,
    lose the Qdrant 'volume' (a fresh empty server), run rebuild_index, and
    require overlap recall ≥ 0.99 (TITAN-BRN-004 acceptance). The live DoD
    repeats this against a real Qdrant container with the volume deleted."""
    harness = _Harness(api, "on")
    dataset = evaluation.load_dataset(DATASET_PATH)

    async def scenario() -> float:
        await harness.seed()
        # Retire the shared DB's residue for THIS gate: other tests seed
        # textually IDENTICAL records repeatedly, and identical text means
        # identical vectors — cosine TIES whose order is index-insertion-
        # dependent. Comparing top-10s across two index builds would then
        # measure tie churn, not rebuild fidelity (the live DoD runs this
        # gate unfiltered on a clean corpus). Rebuild treats non-active
        # records' points as strays, so the corpus is exactly ours.
        async with harness.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE brain.memory_records SET status = 'superseded' "
                    "WHERE status = 'active' AND NOT (id = ANY(CAST(:ids AS uuid[])))"
                ),
                {"ids": [str(record_id) for record_id in harness.ids.values()]},
            )
        # Reconcile FIRST so the baseline reflects a complete, converged index.
        await rebuild_index(harness.engine, harness.pipeline.qdrant, harness.pipeline.embeddings)
        before = [(await harness.run_query(pair))[: evaluation.EVAL_K] for pair in dataset.pairs]
        # The volume is gone: a brand-new empty Qdrant.
        harness.pipeline.server = FakeQdrantServer()
        harness.pipeline.qdrant = QdrantIndex(harness.pipeline.server.client())
        report = await rebuild_index(
            harness.engine, harness.pipeline.qdrant, harness.pipeline.embeddings
        )
        assert report.collections["titan_memory"].points > 0
        after = [(await harness.run_query(pair))[: evaluation.EVAL_K] for pair in dataset.pairs]
        return evaluation.overlap_recall(before, after)

    recall = run_async(scenario)
    _report(f"MEMORY-EVAL rebuild overlap recall@10={recall:.4f} (gate ≥ 0.99)")
    assert recall >= evaluation.REBUILD_MIN_RECALL
