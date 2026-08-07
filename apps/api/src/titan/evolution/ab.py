"""A/B harness: shadow/canary assignment + arm recording + sequential analysis
(Vol 10 §6, TITAN-EVO-018).

Assignment is a DETERMINISTIC hash of the task id (so a task lands in the same cohort
on every replica/redelivery). Each arm's score is computed through the SAME Phase-84
composite (no second metrics store) and recorded in ``evolution_ab_arms``. The analysis
is a sequential (Welch-style) comparison that emits a promote/abort/continue
recommendation — advisory only; promotion/abort execute solely through the human gate.

SCOPING (honest gap, Phase 86): running the challenger against LIVE task traffic needs a
variant-aware prompt resolver that does not exist (the resolver serves a single fail-closed
champion). This module provides the assignment + arm-recording + analysis as real, tested
seams; the live challenger-execution wiring is documented as a follow-up in docs/roadmap.md.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.db import uuid7


def ab_bucket(task_id: uuid.UUID, pct: int) -> bool:
    """Deterministic canary membership: True iff this task falls in the bottom ``pct``%
    of a stable sha256 hash of the task id. pct<=0 → never; pct>=100 → always."""
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    digest = hashlib.sha256(str(task_id).encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:4], "big") % 100  # 0..99
    return bucket < pct


def assigned_arms(mode: str, canary_pct: int, task_id: uuid.UUID) -> list[str]:
    """Which arms EVALUATE this task.

    - shadow: BOTH arms always (challenger scored in parallel, never served).
    - canary: the challenger arm only for the canary cohort, else the champion arm.
    """
    if mode == "shadow":
        return ["champion", "challenger"]
    return ["challenger"] if ab_bucket(task_id, canary_pct) else ["champion"]


async def record_arm(
    engine: AsyncEngine,
    *,
    deployment_id: uuid.UUID,
    task_id: uuid.UUID,
    arm: str,
    mode: str,
    agent_id: str,
    task_class: str,
    composite_score: float,
    dimensions: dict[str, int],
    prompt_sha: str,
    model_id: str,
) -> bool:
    """Record one arm's score for a task (idempotent per (deployment, task, arm)).
    Returns True if a row was inserted, False if it already existed."""
    import json as _json

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO evolution_ab_arms (id, deployment_id, task_id, arm, mode, agent_id, "
                "task_class, composite_score, dimensions, prompt_sha, model_id) VALUES "
                "(:id,:dep,:tid,:arm,:mode,:aid,:cls,:score,CAST(:dims AS JSONB),:psha,:model) "
                "ON CONFLICT (deployment_id, task_id, arm) DO NOTHING"
            ),
            {
                "id": uuid7(),
                "dep": deployment_id,
                "tid": task_id,
                "arm": arm,
                "mode": mode,
                "aid": agent_id,
                "cls": task_class,
                "score": composite_score,
                "dims": _json.dumps(dimensions),
                "psha": prompt_sha,
                "model": model_id,
            },
        )
        return result.rowcount > 0


async def arm_scores(engine: AsyncEngine, deployment_id: uuid.UUID) -> dict[str, list[float]]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT arm, composite_score FROM evolution_ab_arms "
                    "WHERE deployment_id = :dep ORDER BY created_at"
                ),
                {"dep": deployment_id},
            )
        ).all()
    out: dict[str, list[float]] = {"champion": [], "challenger": []}
    for arm, score in rows:
        out.setdefault(str(arm), []).append(float(score))
    return out


@dataclass
class SequentialResult:
    recommendation: str  # promote | abort | continue
    n_champion: int
    n_challenger: int
    mean_champion: float
    mean_challenger: float
    delta: float  # challenger - champion
    z: float  # Welch z of the delta (0 when undersampled)
    reason: str


def _mean_std(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    mean = sum(xs) / n
    if n < 2:
        return mean, 0.0
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return mean, math.sqrt(var)


def sequential_test(
    champion: list[float],
    challenger: list[float],
    *,
    min_samples: int = 5,
    superiority_margin: float = 2.0,
    z_threshold: float = 1.96,
) -> SequentialResult:
    """A bounded Welch-style sequential comparison (SPRT-flavoured, TITAN-EVO-018).

    Undersampled → ``continue``. Otherwise compute the Welch z of (challenger-champion):
    a significant positive delta beyond ``superiority_margin`` → ``promote``; a significant
    negative delta → ``abort``; else ``continue``. Advisory only — the human gate executes."""
    nc, nt = len(champion), len(challenger)
    mc, sc = _mean_std(champion)
    mt, st = _mean_std(challenger)
    delta = mt - mc
    if nc < min_samples or nt < min_samples:
        return SequentialResult(
            "continue", nc, nt, mc, mt, delta, 0.0, "insufficient samples for a decision"
        )
    se = math.sqrt((sc**2 / nc if nc else 0.0) + (st**2 / nt if nt else 0.0))
    z = delta / se if se > 1e-9 else (math.copysign(999.0, delta) if abs(delta) > 1e-9 else 0.0)
    if z >= z_threshold and delta >= superiority_margin:
        return SequentialResult(
            "promote", nc, nt, mc, mt, delta, z, "challenger significantly better"
        )
    if z <= -z_threshold:
        return SequentialResult("abort", nc, nt, mc, mt, delta, z, "challenger significantly worse")
    return SequentialResult("continue", nc, nt, mc, mt, delta, z, "no significant difference yet")


async def analyze_deployment(
    engine: AsyncEngine, deployment_id: uuid.UUID, **kw: Any
) -> SequentialResult:
    scores = await arm_scores(engine, deployment_id)
    return sequential_test(scores.get("champion", []), scores.get("challenger", []), **kw)


__all__ = [
    "SequentialResult",
    "ab_bucket",
    "analyze_deployment",
    "arm_scores",
    "assigned_arms",
    "record_arm",
    "sequential_test",
]
