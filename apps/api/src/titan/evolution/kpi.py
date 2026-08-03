"""KPI rollup + degradation detection (Vol 10 §3, TITAN-EVO-006/007).

The nightly ``evolution.kpi-rollup`` job aggregates scorecards into per-agent,
per-task-class KPIs over 7/30/90-day windows, publishes them as Prometheus
``titan_agent_kpi`` gauges (+ the ``GET /evolution/agents/{id}/kpis`` endpoint),
and flags a sustained >10% drop vs the trailing 30-day mean by opening an
``evolution_investigations`` record for the Memory Agent and lighting the
``titan_agent_kpi_degraded`` gauge the alert rule fires on.

Aggregation reads scorecards directly, so the rollup reconciles with the raw
scorecards exactly (DoD ±0.5%). Pure seams: :func:`aggregate`, :func:`detect`.
"""

import asyncio
import contextlib
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.evolution import repository as repo
from titan.evolution.scoring import DIMENSIONS
from titan.observability.metrics import AGENT_KPI, AGENT_KPI_DEGRADED

logger = structlog.get_logger("titan.evolution.kpi")

WINDOWS = (7, 30, 90)
_RECENT_DAYS = 3  # approximates "3 consecutive days" (§3, EVO-007) as a 3-day window
_BASELINE_DAYS = 30
_DROP_THRESHOLD = 0.10  # >10% below the trailing mean
_MIN_SAMPLES = 5  # min BASELINE samples — ignore small-sample noise (§13.2)
_MIN_RECENT = 2  # min RECENT samples — a single unlucky task must not open an investigation
_ALL = "all"


def aggregate(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Mean composite + per-dimension means + count per (agent, task_class), plus a
    per-agent (task_class='all') rollup. Means come straight from the scorecards, so
    the rollup reconciles with the raw rows exactly."""
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        for key in ((r["agent_id"], r["task_class"]), (r["agent_id"], _ALL)):
            buckets.setdefault(key, []).append(r)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, group in buckets.items():
        n = len(group)
        composite = sum(g["composite_score"] for g in group) / n
        dims = {d: sum(g["dimensions"].get(d, 0) for g in group) / n for d in DIMENSIONS}
        out[key] = {"count": n, "composite": round(composite, 4), "dimensions": dims}
    return out


async def compute_rollup(engine: AsyncEngine) -> dict[int, dict[tuple[str, str], dict[str, Any]]]:
    """Aggregate every window + publish the Prometheus gauges. Returns the
    aggregates (for the API endpoint + reconciliation tests)."""
    result: dict[int, dict[tuple[str, str], dict[str, Any]]] = {}
    # Clear stale label series first: an (agent, task_class) that has aged out of
    # every window must not linger at its last-published value on the dashboard (M6).
    AGENT_KPI.clear()
    for days in WINDOWS:
        agg = aggregate(await repo.scorecards_since(engine, days=days))
        result[days] = agg
        window = f"{days}d"
        for (agent, task_class), stats in agg.items():
            AGENT_KPI.labels(agent, task_class, "composite", window).set(stats["composite"])
            for dim, value in stats["dimensions"].items():
                AGENT_KPI.labels(agent, task_class, dim, window).set(round(value, 4))
    logger.info("kpi.rollup_computed", windows=list(WINDOWS))
    return result


def detect(
    recent: dict[tuple[str, str], dict[str, Any]],
    baseline: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Per (agent, task_class): a sustained drop = recent composite >10% below the
    trailing-30d mean, with enough baseline samples. Skips the 'all' rollup (per
    §3 the KPI is per-agent/per-class)."""
    out: list[dict[str, Any]] = []
    for key, base in baseline.items():
        agent, task_class = key
        if task_class == _ALL or base["count"] < _MIN_SAMPLES:
            continue
        rec = recent.get(key)
        if rec is None or rec["count"] < _MIN_RECENT:
            continue
        base_c, rec_c = base["composite"], rec["composite"]
        if base_c <= 0:
            continue
        drop = (base_c - rec_c) / base_c
        if drop > _DROP_THRESHOLD:
            out.append(
                {
                    "agent_id": agent,
                    "task_class": task_class,
                    "window_days": _RECENT_DAYS,
                    "recent_value": round(rec_c, 2),
                    "baseline_value": round(base_c, 2),
                    "drop_pct": round(drop * 100, 2),
                    "series_snapshot": {
                        "recent": {"days": _RECENT_DAYS, **rec},
                        "baseline": {"days": _BASELINE_DAYS, **base},
                    },
                }
            )
    return out


async def scan_degradations(engine: AsyncEngine) -> list[dict[str, Any]]:
    """Detect degradations, light the alert gauge, and open an investigation for
    each (idempotent). Returns the degradations detected this scan."""
    recent = aggregate(await repo.scorecards_since(engine, days=_RECENT_DAYS))
    # The baseline EXCLUDES the recent window (else the tanking recent rows dilute it
    # and mask the very drop we are trying to detect — a fail-open, H2).
    baseline = aggregate(
        await repo.scorecards_since(engine, days=_BASELINE_DAYS, before_days=_RECENT_DAYS)
    )
    degradations = detect(recent, baseline)
    degraded_keys = {(d["agent_id"], d["task_class"]) for d in degradations}
    # Clear stale degraded flags first, then light only the current ones (M6): an
    # aged-out agent must not stay flagged degraded forever.
    AGENT_KPI_DEGRADED.clear()
    # Light/clear the alert gauge for every (agent, class) we have a baseline for.
    for agent, task_class in baseline:
        if task_class == _ALL:
            continue
        AGENT_KPI_DEGRADED.labels(agent, task_class).set(
            1 if (agent, task_class) in degraded_keys else 0
        )
    for deg in degradations:
        opened = await repo.open_investigation(
            engine,
            agent_id=deg["agent_id"],
            task_class=deg["task_class"],
            window_days=deg["window_days"],
            recent_value=deg["recent_value"],
            baseline_value=deg["baseline_value"],
            drop_pct=deg["drop_pct"],
            series_snapshot=deg["series_snapshot"],
        )
        if opened:
            logger.warning(
                "kpi.degradation_investigation_opened",
                agent=deg["agent_id"],
                task_class=deg["task_class"],
                drop_pct=deg["drop_pct"],
                investigation_id=opened,
            )
    return degradations


async def agent_kpis(engine: AsyncEngine, agent_id: str) -> dict[str, Any]:
    """Per-window (7/30/90d) KPI aggregates for one agent, for the API endpoint."""
    windows: dict[str, Any] = {}
    for days in WINDOWS:
        agg = aggregate(await repo.scorecards_since(engine, days=days, agent_id=agent_id))
        windows[f"{days}d"] = {
            (task_class if task_class != _ALL else "all"): stats
            for (agent, task_class), stats in agg.items()
        }
    return {"agent_id": agent_id, "windows": windows}


async def run_kpi_rollup(  # pragma: no cover - loop verified via its units
    engine: AsyncEngine, *, stop: asyncio.Event, interval_s: float
) -> None:
    """Leader-only singleton: roll up KPIs + scan for degradations on an interval
    (nightly by default). The per-iteration units are unit-tested."""
    while not stop.is_set():
        try:
            await compute_rollup(engine)
            await scan_degradations(engine)
        except Exception:
            logger.exception("kpi.rollup_loop_error")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)


__all__ = [
    "WINDOWS",
    "agent_kpis",
    "aggregate",
    "compute_rollup",
    "detect",
    "run_kpi_rollup",
    "scan_degradations",
]
