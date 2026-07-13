"""Estimation feedback (Vol 6 §4.1, TITAN-WFL-017). PURE — the correction-factor
math is side-effect-free; ``service`` persists the factor to organization memory
and reads it back to recalibrate future estimates.

Story-point estimates are recalibrated per project from actuals: for each completed
task we form the ratio ``actual_effort / estimated_effort`` and take the median (the
robust central tendency — one runaway task should not skew the factor). A factor >1
means the team consistently under-estimates; recalibration multiplies raw estimates
by it so the next sprint's numbers track reality.
"""

from dataclasses import dataclass

#: Below this many samples the factor is not trustworthy; callers keep 1.0 (no
#: adjustment) until a project has enough history.
MIN_SAMPLES: int = 3


@dataclass(frozen=True)
class EstimateActual:
    """One completed task's estimated vs. actual effort (any consistent unit —
    ideal-days, loop cycles, or wall-clock hours; the ratio is unit-free)."""

    estimate: float
    actual: float


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def correction_factor(samples: list[EstimateActual]) -> float:
    """The median actual/estimate ratio over samples with a positive estimate.
    Returns 1.0 (no adjustment) below :data:`MIN_SAMPLES` or with no valid sample."""
    ratios = [s.actual / s.estimate for s in samples if s.estimate > 0 and s.actual >= 0]
    if len(ratios) < MIN_SAMPLES:
        return 1.0
    return _median(ratios)


def recalibrate(points: float, factor: float) -> float:
    """Apply a correction factor to a raw estimate. A non-positive factor is
    treated as no-adjustment (1.0) to stay safe against a degenerate history."""
    return points * (factor if factor > 0 else 1.0)


def median_abs_pct_error(samples: list[EstimateActual]) -> float:
    """Median absolute percentage error of the estimates — the WFL-017 benchmark
    metric a project's recalibration should drive down. Returns 0.0 with no
    valid sample."""
    errors = [abs(s.actual - s.estimate) / s.estimate * 100.0 for s in samples if s.estimate > 0]
    if not errors:
        return 0.0
    return _median(errors)


__all__ = [
    "MIN_SAMPLES",
    "EstimateActual",
    "correction_factor",
    "median_abs_pct_error",
    "recalibrate",
]
