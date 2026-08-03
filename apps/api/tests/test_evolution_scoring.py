"""Scorecard scoring logic (Phase 84, Vol 10 §3) — pure unit tests, no DB.

Covers the composite weighting, task-class derivation, non-executor evaluator
selection, the objective/rubric blend + correctness anchor, and rubric parsing.
"""

from pathlib import Path

from titan.evolution.scoring import (
    DIMENSIONS,
    blend_dimensions,
    choose_evaluator,
    composite_score,
    load_weights,
    parse_rubric,
    task_class_for_role,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _flat(v: int = 80) -> dict[str, int]:
    return {d: v for d in DIMENSIONS}


def test_weights_load_and_each_class_sums_to_one() -> None:
    w = load_weights(REPO_ROOT)
    for task_class, weights in w.per_class.items():
        assert abs(sum(weights.values()) - 1.0) < 1e-9, f"{task_class} weights must sum to 1.0"
    assert "default" in w.per_class


def test_task_class_from_role() -> None:
    w = load_weights(REPO_ROOT)
    assert task_class_for_role(w, "eng_backend") == "code"
    assert task_class_for_role(w, "reviewer") == "review"
    assert task_class_for_role(w, "security") == "security"
    assert task_class_for_role(w, "nonesuch") == "default"


def test_composite_is_weighted_mean() -> None:
    w = load_weights(REPO_ROOT)
    # All dims 80 → composite 80 regardless of the weights (they sum to 1).
    assert float(composite_score(_flat(80), w.for_class("code"))) == 80.0
    # A weighted case: security-heavy class rewards a high security score.
    dims = {**_flat(50), "security": 100}
    sec = float(composite_score(dims, w.for_class("security")))
    assert sec > 50.0  # security weight (0.30) pulls it up


def test_choose_evaluator_is_never_the_executor() -> None:
    assert choose_evaluator("code", "eng_backend") == "reviewer"
    assert choose_evaluator("plan", "architect") == "pm_project"
    # The executor IS the default evaluator → a different qualified agent is chosen.
    assert choose_evaluator("review", "reviewer") == "pm_project"
    assert choose_evaluator("plan", "pm_project") == "reviewer"


def test_blend_overrides_objective_and_anchors_correctness() -> None:
    rubric = {**_flat(90), "correctness": 95}
    computed = {"first_pass_quality": 40, "efficiency": 30, "cost": 20}
    out = blend_dimensions(rubric, computed=computed, terminal_status="done")
    # Objective dims are overridden by telemetry...
    assert out["first_pass_quality"] == 40 and out["efficiency"] == 30 and out["cost"] == 20
    # ...judgement dims keep the rubric value.
    assert out["standards_compliance"] == 90
    # A failed task cannot score high on correctness regardless of the rubric.
    failed = blend_dimensions(rubric, computed=computed, terminal_status="failed")
    assert failed["correctness"] <= 30


def test_parse_rubric_handles_json_and_garbage() -> None:
    good = '{"correctness": 88, "security": 70, "collaboration": 60}'
    parsed = parse_rubric(f"Here you go: {good}")
    assert parsed["correctness"] == 88 and parsed["security"] == 70
    assert parse_rubric("not json at all") == {}
    assert parse_rubric(None) == {}
    # Out-of-range values are clamped.
    assert parse_rubric('{"correctness": 999}')["correctness"] == 100
