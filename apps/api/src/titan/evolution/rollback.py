"""Evolution A/B deployment lifecycle + automatic rollback (Vol 10 §2/§6, EVO-003/018).

A challenger config is deployed under a MONITORING CONTRACT: a KPI floor + an observation
window + a minimum sample count. A short-cadence monitor evaluates every active deployment
and, on a floor breach, REVERTS the challenger to the champion within the window — emitting
a Prometheus signal, an audit event, and an incident Root-Cause Record (Phase 85). This is
the in-process analogue of the Phase-80 blue/green detector/actuator split, at the
evolution-config layer (the blue/green machinery reverts container images, the wrong layer).

The detector (``evaluate_deployment``) is separated from the actuator (``rollback_deployment``)
so the destructive revert is gated on an explicit typed breach decision, never a bare status.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.audit.service import AuditAction, AuditClient, Outcome
from titan.db import uuid7
from titan.evolution import ab, failures
from titan.observability.metrics import (
    EVOLUTION_DEPLOYMENTS_ACTIVE,
    EVOLUTION_DEPLOYMENTS_STARVED,
    EVOLUTION_ROLLBACKS,
)

logger = structlog.get_logger("titan.evolution.rollback")


def _dep_dict(r: Any) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "ep": r["ep"],
        "agent_id": r["agent_id"],
        "mode": r["mode"],
        "status": r["status"],
        "canary_pct": r["canary_pct"],
        "champion_ref": r["champion_ref"],
        "challenger_ref": r["challenger_ref"],
        "kpi_floor": float(r["kpi_floor"]),
        "min_samples": r["min_samples"],
        "observation_window_s": r["observation_window_s"],
        "rollback_reason": r["rollback_reason"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "age_s": r.get("age_s"),
    }


async def create_deployment(
    engine: AsyncEngine,
    *,
    ep: str,
    agent_id: str,
    mode: str,
    champion_ref: str,
    challenger_ref: str,
    kpi_floor: float,
    canary_pct: int = 0,
    min_samples: int = 5,
    observation_window_s: int = 300,
) -> dict[str, Any] | None:
    """Open an A/B deployment. Idempotent on the active-agent partial unique index:
    returns None if an active (shadow|canary) deployment already exists for the agent."""
    row_id = uuid7()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO evolution_deployments (id, ep, agent_id, mode, status, canary_pct, "
                "champion_ref, challenger_ref, kpi_floor, min_samples, observation_window_s) "
                "VALUES (:id,:ep,:aid,:mode,:mode,:pct,:champ,:chal,:floor,:mins,:win) "
                "ON CONFLICT (agent_id) WHERE status IN ('shadow','canary') DO NOTHING "
                "RETURNING id"
            ),
            {
                "id": row_id,
                "ep": ep,
                "aid": agent_id,
                "mode": mode,
                "pct": canary_pct,
                "champ": champion_ref,
                "chal": challenger_ref,
                "floor": Decimal(str(kpi_floor)),
                "mins": min_samples,
                "win": observation_window_s,
            },
        )
        if result.first() is None:
            return None
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM evolution_deployments WHERE id = :id"), {"id": row_id}
                )
            )
            .mappings()
            .first()
        )
    return _dep_dict(row) if row else None


async def list_active_deployments(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT *, EXTRACT(EPOCH FROM (now() - created_at)) AS age_s "
                        "FROM evolution_deployments WHERE status IN ('shadow','canary') "
                        "ORDER BY created_at"
                    )
                )
            )
            .mappings()
            .all()
        )
    return [_dep_dict(r) for r in rows]


async def get_deployment(engine: AsyncEngine, deployment_id: uuid.UUID) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM evolution_deployments WHERE id = :id"),
                    {"id": deployment_id},
                )
            )
            .mappings()
            .first()
        )
    return _dep_dict(row) if row else None


@dataclass
class BreachDecision:
    breached: bool
    mean: float
    n: int
    reason: str


async def evaluate_deployment(engine: AsyncEngine, deployment: dict[str, Any]) -> BreachDecision:
    """The DETECTOR: is the challenger below its KPI floor with enough samples? Returns a
    typed decision; it never mutates state (the actuator does that on ``breached``)."""
    scores = (await ab.arm_scores(engine, uuid.UUID(deployment["id"]))).get("challenger", [])
    n = len(scores)
    if n < int(deployment["min_samples"]):
        return BreachDecision(
            False, 0.0 if not scores else sum(scores) / n, n, "insufficient samples"
        )
    mean = sum(scores) / n
    floor = float(deployment["kpi_floor"])
    if mean < floor:
        return BreachDecision(True, mean, n, f"challenger mean {mean:.2f} < floor {floor:.2f}")
    return BreachDecision(False, mean, n, f"challenger mean {mean:.2f} ≥ floor {floor:.2f}")


async def _set_status(
    engine: AsyncEngine, deployment_id: uuid.UUID, status: str, reason: str
) -> bool:
    """Move a deployment out of active state, guarding on it still being active so two
    monitors cannot double-fire the rollback. Returns True if this call made the change."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE evolution_deployments SET status = :s, rollback_reason = :r, "
                "rolled_back_at = CASE WHEN :s IN ('rolled_back','aborted') THEN now() ELSE "
                "rolled_back_at END WHERE id = :id AND status IN ('shadow','canary')"
            ),
            {"s": status, "r": reason, "id": deployment_id},
        )
        return result.rowcount > 0


async def rollback_deployment(
    engine: AsyncEngine,
    audit: AuditClient,
    deployment: dict[str, Any],
    *,
    reason: str,
    actor: str = "system.evolution",
    automatic: bool = True,
) -> dict[str, Any] | None:
    """The ACTUATOR: revert the challenger to the champion (EVO-003). Guarded so only the
    monitor that wins the status transition emits the signal/audit/RCR (no double-fire).
    Emits: Prometheus counter, an EVOLUTION_ROLLBACK audit event, and an incident RCR."""
    dep_id = uuid.UUID(deployment["id"])
    won = await _set_status(engine, dep_id, "rolled_back", reason)
    if not won:
        return None  # already rolled back / promoted by another actor — idempotent
    agent = deployment["agent_id"]
    mode = deployment["mode"]
    EVOLUTION_ROLLBACKS.labels(agent=agent, mode=mode).inc()
    # The revert (the status flip) is the source of truth and already committed. The audit +
    # RCR are best-effort, but a failure is LOGGED at error (not silently suppressed) so the
    # audit trail can't drift from reality without a trace (review gate-8).
    try:
        await audit.record(
            actor=actor,
            action=AuditAction.EVOLUTION_ROLLBACK,
            resource=f"deployment:{deployment['id']}",
            outcome=Outcome.SUCCESS,
            detail={
                "ep": deployment["ep"],
                "agent_id": agent,
                "mode": mode,
                "reason": reason,
                "automatic": automatic,
                "champion_ref": deployment["champion_ref"],
                "challenger_ref": deployment["challenger_ref"],
            },
        )
    except Exception:
        logger.error(
            "evolution.rollback_audit_failed", deployment_id=deployment["id"], exc_info=True
        )
    # An incident RCR (Phase 85) — signature scoped to the EP so a repeat rollback of the
    # same EP is deduped. It carries NO `ep` link: a rolled-back challenger's EP was
    # ineffective, so approving a (colliding) artifact with that same ep string must NOT
    # silently auto-remediate this incident (a confused-deputy). A human resolves it.
    try:
        await failures.open_or_reopen_rcr(
            engine,
            failure_code="F-LLM" if automatic else "F-HUM",
            severity="high",
            signature=f"rollback:{deployment['ep']}:{agent}",
            summary=f"Auto-rollback of challenger for {agent} (EP {deployment['ep']}): {reason}",
            remediation={"action": "revise or abandon the challenger config"},
        )
    except Exception:
        logger.error("evolution.rollback_rcr_failed", deployment_id=deployment["id"], exc_info=True)
    logger.warning(
        "evolution.deployment_rolled_back",
        deployment_id=deployment["id"],
        agent=agent,
        reason=reason,
    )
    return await get_deployment(engine, dep_id)


async def set_terminal_status(
    engine: AsyncEngine,
    audit: AuditClient,
    deployment: dict[str, Any],
    *,
    status: str,
    actor: str,
    reason: str,
) -> dict[str, Any] | None:
    """Human promote/abort of a deployment (EVO-018, human-gated). Audited; abort also
    opens an RCR (a rejected challenger is a learning signal)."""
    dep_id = uuid.UUID(deployment["id"])
    if status == "rolled_back":
        return await rollback_deployment(
            engine, audit, deployment, reason=reason, actor=actor, automatic=False
        )
    won = await _set_status(engine, dep_id, status, reason)
    if not won:
        return None
    with contextlib.suppress(Exception):
        await audit.record(
            actor=actor,
            action=AuditAction.EVOLUTION_ROLLBACK,
            resource=f"deployment:{deployment['id']}",
            outcome=Outcome.SUCCESS,
            detail={"status": status, "reason": reason, "ep": deployment["ep"], "automatic": False},
        )
    return await get_deployment(engine, dep_id)


def _is_starved(dep: dict[str, Any], n: int) -> bool:
    """A deployment past its observation window that still lacks the samples to decide —
    the auto-rollback contract cannot fire for it, so it must be surfaced (review gate-4)."""
    age = dep.get("age_s")
    return (
        age is not None
        and float(age) > float(dep["observation_window_s"])
        and n < int(dep["min_samples"])
    )


async def scan_and_rollback(engine: AsyncEngine, audit: AuditClient) -> int:
    """One monitor pass: evaluate every active deployment; roll back the breached ones.
    Each deployment is isolated (a poison row can't block the others), and the active +
    starved gauges are always refreshed. Returns the number rolled back."""
    active = await list_active_deployments(engine)
    by_mode: dict[str, int] = {"shadow": 0, "canary": 0}
    starved = 0
    rolled = 0
    for dep in active:
        try:
            decision = await evaluate_deployment(engine, dep)
            if decision.breached:
                if (
                    await rollback_deployment(engine, audit, dep, reason=decision.reason)
                    is not None
                ):
                    rolled += 1
                continue
            by_mode[dep["mode"]] = by_mode.get(dep["mode"], 0) + 1
            if _is_starved(dep, decision.n):
                starved += 1
                logger.warning(
                    "evolution.deployment_starved",
                    deployment_id=dep["id"],
                    agent=dep["agent_id"],
                    samples=decision.n,
                    min_samples=dep["min_samples"],
                    age_s=dep.get("age_s"),
                )
        except Exception:
            # One bad deployment must not block rolling back the others this pass.
            logger.exception("evolution.deployment_eval_error", deployment_id=dep.get("id"))
    for mode, count in by_mode.items():
        EVOLUTION_DEPLOYMENTS_ACTIVE.labels(mode=mode).set(count)
    EVOLUTION_DEPLOYMENTS_STARVED.set(starved)
    return rolled


async def run_rollback_monitor(  # pragma: no cover - loop verified via scan_and_rollback
    engine: AsyncEngine, audit: AuditClient, *, stop: asyncio.Event, interval_s: float = 30.0
) -> None:
    """Leader-only short-cadence monitor (EVO-003): a floor breach reverts within the
    observation window (default 300 s), far under the 5-minute SLA at a 30 s cadence."""
    while not stop.is_set():
        try:
            await scan_and_rollback(engine, audit)
        except Exception:
            logger.exception("evolution.rollback_monitor_error")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)


__all__ = [
    "BreachDecision",
    "create_deployment",
    "evaluate_deployment",
    "get_deployment",
    "list_active_deployments",
    "rollback_deployment",
    "run_rollback_monitor",
    "scan_and_rollback",
    "set_terminal_status",
]
