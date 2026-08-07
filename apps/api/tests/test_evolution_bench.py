"""TITAN-Bench + the offline gate (Phase 86, Vol 10 §6, EVO-016/017/ADR-EVO-02).

Proves the DoD anchor 1: an EP PR with a failing (or missing) bench report cannot pass
the gate, and one that passes gates.yaml can. Plus the pure scorers, gate non-inferiority,
report round-trip, and B-REG case generation from live Root-Cause Records.
"""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from scripts.check_bench import collect_errors

from tests.conftest import run_async
from titan.evolution import bench, failures

pytestmark_db = pytest.mark.schema


# ---- pure: scorers + gate ------------------------------------------------------------


def test_track_scorers() -> None:
    suite = bench.load_bench_suite()
    code = next(c for c in suite.cases if c.track == "B-CODE")
    assert bench.score_case(code, {"tests_passed": code.answer["hidden_tests_total"]}) == 1.0
    assert bench.score_case(code, {"tests_passed": 0}) == 0.0

    review = next(c for c in suite.cases if c.track == "B-REVIEW")
    seeded = review.answer["seeded_defects"]
    assert bench.score_case(review, {"found": seeded}) == 1.0
    assert bench.score_case(review, {"found": seeded[:1]}) == pytest.approx(1 / len(seeded))

    sec_vuln = next(c for c in suite.cases if c.id == "sec-vuln-eval")
    assert bench.score_case(sec_vuln, {"label": "vuln"}) == 1.0
    assert bench.score_case(sec_vuln, {"label": "safe"}) == 0.0

    reg = next(c for c in suite.cases if c.track == "B-REG")
    assert bench.score_case(reg, {"recurred": False}) == 1.0
    assert bench.score_case(reg, {"recurred": True}) == 0.0
    # Fail-closed: a non-answer (no `recurred` key) must NOT clear the zero-margin B-REG.
    assert bench.score_case(reg, {}) == 0.0


def test_run_bench_and_report_round_trip() -> None:
    suite = bench.load_bench_suite()

    def perfect(case: bench.BenchCase) -> dict[str, Any]:
        a = case.answer
        return {
            "B-CODE": {"tests_passed": a.get("hidden_tests_total", 0)},
            "B-REVIEW": {"found": a.get("seeded_defects", [])},
            "B-PLAN": {"nodes": a.get("nodes", []), "edges": a.get("edges", [])},
            "B-ARCH": {"mentioned": a.get("must_mention", [])},
            "B-SEC": {"label": a.get("label", "")},
            "B-REG": {"recurred": False},
        }[case.track]

    report = bench.run_bench(suite, perfect, manifest_sha="abc", label="champion")
    assert report.overall == pytest.approx(1.0)
    assert set(report.per_track) == set(bench.TRACKS)
    # A round-trip through the JSON shape preserves scores.
    back = bench.BenchReport.from_dict(json.loads(json.dumps(report.to_dict())))
    assert back.track_score("B-SEC") == pytest.approx(1.0)


def test_crashing_solver_scores_zero() -> None:
    suite = bench.load_bench_suite()

    def boom(case: bench.BenchCase) -> dict[str, Any]:
        raise RuntimeError("solver down")

    report = bench.run_bench(suite, boom)
    assert report.overall == 0.0


def test_gate_non_inferiority_and_floor() -> None:
    gates = bench.load_gates()
    champ = bench.BenchReport.from_dict({"suite_version": 1, "tracks": _champion_tracks()})
    # Within the margin on the tolerant tracks, MATCHING on the zero-margin ones → passes.
    ok_tracks = {t: {"score": 0.9, "n": 1} for t in bench.TRACKS}
    ok_tracks["B-SEC"] = {"score": 1.0, "n": 1}
    ok_tracks["B-REG"] = {"score": 1.0, "n": 1}
    ok = bench.BenchReport.from_dict({"suite_version": 1, "tracks": ok_tracks})
    assert bench.evaluate_gate(ok, gates, champion=champ).passed
    bad = bench.BenchReport.from_dict(
        {
            "suite_version": 1,
            "tracks": {
                **{t: {"score": 0.95, "n": 1} for t in bench.TRACKS},
                "B-SEC": {"score": 0.9, "n": 1},
            },
        }
    )
    outcome = bench.evaluate_gate(bad, gates, champion=champ)
    assert not outcome.passed and any("B-SEC" in f for f in outcome.failures)


def test_gate_missing_track_fails_closed() -> None:
    gates = bench.load_gates()
    partial = bench.BenchReport.from_dict(
        {"suite_version": 1, "tracks": {"B-CODE": {"score": 1.0, "n": 1}}}
    )
    outcome = bench.evaluate_gate(partial, gates)
    assert not outcome.passed and any("B-SEC" in f for f in outcome.failures)


# ---- DoD anchor 1: the CI gate blocks a failing/missing report, passes a good one ----


def _champion_tracks() -> dict[str, Any]:
    # Security + regression carry zero margin, so a non-inferior challenger must MATCH the
    # champion there — bake that into the baseline (mirrors the committed champion.json).
    tracks = {t: {"score": 0.95, "n": 1} for t in bench.TRACKS}
    tracks["B-SEC"] = {"score": 1.0, "n": 2}
    tracks["B-REG"] = {"score": 1.0, "n": 1}
    return tracks


def _write_bench_tree(root: Path, challenger: dict[str, Any] | None) -> None:
    (root / "evolution" / "bench" / "reports").mkdir(parents=True, exist_ok=True)
    # repo root: apps/api/src/titan/evolution/bench.py -> parents[5].
    real_gates = Path(bench.__file__).resolve().parents[5] / "evolution" / "bench" / "gates.yaml"
    (root / "evolution" / "bench" / "gates.yaml").write_text(real_gates.read_text("utf-8"), "utf-8")
    champion = {"suite_version": 1, "tracks": _champion_tracks()}
    (root / "evolution" / "bench" / "reports" / "champion.json").write_text(
        json.dumps(champion), "utf-8"
    )
    if challenger is not None:
        challenger = {"manifest_sha": "test-sha", **challenger}  # a real run stamps this
        (root / "evolution" / "bench" / "reports" / "challenger.json").write_text(
            json.dumps(challenger), "utf-8"
        )


def test_ci_gate_blocks_missing_report_when_required(tmp_path: Path) -> None:
    _write_bench_tree(tmp_path, challenger=None)
    # A prompt-changing PR with NO report → blocked.
    errs = collect_errors(tmp_path, require_report=True)
    assert errs and "no challenger bench report" in errs[0]
    # No EP under review (no prompt change) → nothing to gate.
    assert collect_errors(tmp_path, require_report=False) == []


def test_ci_gate_blocks_failing_report_and_passes_good(tmp_path: Path) -> None:
    failing = {
        "suite_version": 1,
        "tracks": {
            **{t: {"score": 0.95, "n": 1} for t in bench.TRACKS},
            "B-SEC": {"score": 0.5, "n": 1},
        },
    }
    _write_bench_tree(tmp_path, challenger=failing)
    errs = collect_errors(tmp_path, require_report=True)
    assert errs and any("B-SEC" in e for e in errs)

    passing_tracks = {t: {"score": 0.92, "n": 1} for t in bench.TRACKS}
    passing_tracks["B-SEC"] = {"score": 1.0, "n": 1}
    passing_tracks["B-REG"] = {"score": 1.0, "n": 1}
    passing = {"suite_version": 1, "tracks": passing_tracks}
    _write_bench_tree(tmp_path, challenger=passing)
    assert collect_errors(tmp_path, require_report=True) == []


def test_ci_gate_requires_manifest_sha_and_champion(tmp_path: Path) -> None:
    good = {t: {"score": 1.0, "n": 1} for t in bench.TRACKS}
    # No manifest_sha on the challenger → a hand-stubbed report is rejected.
    _write_bench_tree(tmp_path, challenger=None)
    (tmp_path / "evolution" / "bench" / "reports" / "challenger.json").write_text(
        json.dumps({"suite_version": 1, "tracks": good}), "utf-8"
    )
    errs = collect_errors(tmp_path, require_report=True)
    assert any("manifest_sha" in e for e in errs)
    # Champion baseline missing → the non-inferiority bar can't silently drop to floors.
    (tmp_path / "evolution" / "bench" / "reports" / "champion.json").unlink()
    (tmp_path / "evolution" / "bench" / "reports" / "challenger.json").write_text(
        json.dumps({"suite_version": 1, "manifest_sha": "s", "tracks": good}), "utf-8"
    )
    assert any("champion.json" in e for e in collect_errors(tmp_path, require_report=True))


def test_ci_gate_rejects_suite_version_mismatch(tmp_path: Path) -> None:
    stale = {"suite_version": 999, "tracks": {t: {"score": 1.0, "n": 1} for t in bench.TRACKS}}
    _write_bench_tree(tmp_path, challenger=stale)
    errs = collect_errors(tmp_path, require_report=True)
    assert errs and any("suite_version" in e for e in errs)


# ---- B-REG cases generated from live RCRs (DB) ---------------------------------------


@pytestmark_db
def test_bench_cases_from_rcrs(migrated_engine: Any) -> None:
    async def scenario() -> None:
        sig = f"F-SEC:agent.security:{uuid.uuid4().hex[:8]}"
        created = await failures.open_or_reopen_rcr(
            migrated_engine, failure_code="F-SEC", severity="high", signature=sig, summary="x"
        )
        assert created is not None
        # Remediate it so it becomes a regression case.
        from titan.evolution import repository as repo

        await repo.override_rcr(migrated_engine, uuid.UUID(created["id"]), status="remediated")
        cases = await bench.bench_cases_from_rcrs(migrated_engine)
        mine = [c for c in cases if c.answer.get("signature") == sig]
        assert mine and mine[0].track == "B-REG"
        # The signature has no OPEN RCR → not recurred → the regression passes.
        assert await bench.signature_recurred(migrated_engine, sig) is False

    run_async(scenario)
