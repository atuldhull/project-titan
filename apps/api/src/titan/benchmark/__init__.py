"""Benchmark harness: the fixed 10-brief suite + the Section-7 KPI runner
(Vol 1 §7, TITAN-FND-007/052, Phase 55).

Executes the one executable brief end-to-end, folds in the chaos suite's crash-
recovery result, and emits a complete Section-7 KPI report. The other nine briefs
ship as fixtures wired into the nightly CI suite, never the per-commit pipeline.
"""

from titan.benchmark.kpi import BriefKpis, KpiReport, MvpThreshold, SuiteKpis
from titan.benchmark.runner import BenchmarkRunner
from titan.benchmark.suite import BenchmarkSuite, Brief, load_suite

__all__ = [
    "BenchmarkRunner",
    "BenchmarkSuite",
    "Brief",
    "BriefKpis",
    "KpiReport",
    "MvpThreshold",
    "SuiteKpis",
    "load_suite",
]
