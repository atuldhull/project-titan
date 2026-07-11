"""Per-project / per-task LLM budget enforcement (TITAN-INT-064, TITAN-OPS-021).

Redis holds cumulative spend counters in micro-dollars (integer-exact). The gate
is ATOMIC with the spend it guards: the pre-request estimate is *reserved* with a
single ``INCRBY`` and the returned total is compared to the cap, so concurrent
calls cannot both slip under it. The reservation is reconciled to the actual cost
at settle time (or released if the call never happened). At 100% a persisted
``budget_hold`` hard-stops further calls; at 80% a once-per-crossing notification
fires (deduped via a Redis flag). A per-project remaining gauge feeds the
LLM-spend dashboard (Vol 9 §10).

Injecting ``redis=None``/``engine=None`` disables budgets (unit tests, zero-config
local) — the gateway then never blocks on spend.
"""

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.llm import metrics
from titan.llm import repository as repo
from titan.llm.config import Budgets
from titan.llm.models import ChatRequest, ImagePart, TextPart, ToolResultPart, ToolUsePart

logger = structlog.get_logger("titan.llm.budget")

_MICRO = 1_000_000
_NOTIFY_PCT = Decimal("0.80")
_IMAGE_TOKENS = 512  # conservative per-image input allowance for the estimate

# A scope reservation: (scope, scope_id) whose counter carries a live estimate.
Reserved = list[tuple[str, uuid.UUID]]


def spend_key(scope: str, scope_id: uuid.UUID) -> str:
    """The Redis rolling-spend counter key for a budget scope. Shared so that
    raising a budget (clearing the hold) can also reset this counter — otherwise
    a stale at-cap counter would make the next reservation re-breach and re-hold."""
    return f"llm:spend:{scope}:{scope_id}"


def estimate_input_tokens(req: ChatRequest) -> int:
    """A cheap, provider-neutral prompt-size estimate (~4 chars/token). Counts
    message content AND tool-definition schemas (both are billed input); an image
    adds a fixed allowance so multimodal prompts aren't systematically low."""
    chars = 0
    image_tokens = 0
    for message in req.messages:
        for part in message.parts():
            if isinstance(part, TextPart):
                chars += len(part.text)
            elif isinstance(part, ToolResultPart):
                chars += len(part.content)
            elif isinstance(part, ToolUsePart):
                chars += len(json.dumps(part.input))
            elif isinstance(part, ImagePart):
                image_tokens += _IMAGE_TOKENS
    for tool in req.tools or []:
        chars += len(tool.name) + len(tool.description) + len(json.dumps(tool.input_schema))
    return max(1, chars // 4 + image_tokens)


@dataclass(frozen=True)
class Breach:
    """A cap the reservation could not fit under."""

    scope: str  # 'project' | 'task'
    scope_id: uuid.UUID
    cap: Decimal


class BudgetManager:
    """Atomic rolling-counter budget gate. All methods no-op when Redis is absent."""

    def __init__(self, redis: Redis | None, engine: AsyncEngine | None) -> None:
        self._redis = redis
        self._engine = engine

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    @staticmethod
    def _key(scope: str, scope_id: uuid.UUID) -> str:
        return spend_key(scope, scope_id)

    @staticmethod
    def _caps(
        budgets: Budgets | None, project_id: uuid.UUID | None, task_id: uuid.UUID | None
    ) -> list[tuple[str, uuid.UUID, Decimal]]:
        if budgets is None:
            return []
        out: list[tuple[str, uuid.UUID, Decimal]] = []
        if budgets.per_task_usd is not None and task_id is not None:
            out.append(("task", task_id, budgets.per_task_usd))
        if budgets.per_project_usd is not None and project_id is not None:
            out.append(("project", project_id, budgets.per_project_usd))
        return out

    async def held_reason(
        self, conn: AsyncConnection, project_id: uuid.UUID | None, task_id: uuid.UUID | None
    ) -> str | None:
        """The reason a project/task is on budget hold, else None."""
        row = await repo.get_hold(conn, project_id=project_id, task_id=task_id)
        return row.reason if row is not None else None

    async def reserve(
        self,
        budgets: Budgets | None,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        estimated_cost: Decimal,
    ) -> tuple[Breach | None, Reserved]:
        """Atomically reserve ``estimated_cost`` against every applicable cap. On
        breach, any partial reservation is rolled back and ``([]`` is returned; on
        success the reserved scopes are returned for later settle/release."""
        if not self.enabled or budgets is None:
            return None, []
        assert self._redis is not None
        micro = int(estimated_cost * _MICRO)
        reserved: Reserved = []
        for scope, scope_id, cap in self._caps(budgets, project_id, task_id):
            new = await self._redis.incrby(self._key(scope, scope_id), micro)
            if new > int(cap * _MICRO):
                await self._redis.decrby(self._key(scope, scope_id), micro)  # release self
                await self.release(reserved, estimated_cost)  # release earlier scopes
                return Breach(scope, scope_id, cap), []
            reserved.append((scope, scope_id))
        return None, reserved

    async def persist_hold(self, conn: AsyncConnection, breach: Breach, reason: str) -> None:
        await repo.set_hold(conn, scope=breach.scope, scope_id=breach.scope_id, reason=reason)

    async def release(self, reserved: Reserved, estimated_cost: Decimal) -> None:
        """Undo a reservation for a call that never happened (dispatch failed)."""
        if not self.enabled or not reserved:
            return
        assert self._redis is not None
        micro = int(estimated_cost * _MICRO)
        for scope, scope_id in reserved:
            await self._redis.decrby(self._key(scope, scope_id), micro)

    async def settle(
        self,
        budgets: Budgets | None,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        reserved: Reserved,
        estimated_cost: Decimal,
        actual_cost: Decimal,
    ) -> None:
        """Reconcile the reservation to the actual cost and enforce thresholds.
        Runs AFTER the ledger commit and is best-effort: a Redis hiccup is logged,
        never surfaced, so metering never fails a served call (llm_usage is the
        durable source of truth)."""
        if not self.enabled:
            return
        assert self._redis is not None
        reserved_set = set(reserved)
        est_micro = int(estimated_cost * _MICRO)
        act_micro = int(actual_cost * _MICRO)
        try:
            for scope, scope_id, cap in self._caps(budgets, project_id, task_id):
                delta = act_micro - est_micro if (scope, scope_id) in reserved_set else act_micro
                total = await self._redis.incrby(self._key(scope, scope_id), delta)
                await self._enforce(scope, scope_id, cap, Decimal(total) / _MICRO)
        except Exception as exc:  # noqa: BLE001 — metering must never fail the call
            logger.warning("budget.settle_failed", error=str(exc))

    async def _enforce(self, scope: str, scope_id: uuid.UUID, cap: Decimal, spent: Decimal) -> None:
        assert self._redis is not None
        if scope == "project":
            metrics.PROJECT_BUDGET_REMAINING_USD.labels(str(scope_id)).set(
                float(max(Decimal(0), cap - spent))
            )
        if spent >= cap and self._engine is not None:
            async with self._engine.begin() as conn:
                await repo.set_hold(
                    conn, scope=scope, scope_id=scope_id, reason=f"{scope} budget reached ({cap})"
                )
            logger.warning("budget.hold_set", scope=scope, scope_id=str(scope_id), cap=str(cap))
        elif spent >= cap * _NOTIFY_PCT:
            # Fire the 80% notification once per crossing (Redis SETNX flag).
            if await self._redis.set(f"{self._key(scope, scope_id)}:notified80", "1", nx=True):
                logger.info(
                    "budget.threshold_80", scope=scope, scope_id=str(scope_id), spent=str(spent)
                )


__all__ = ["BudgetManager", "Breach", "Reserved", "estimate_input_tokens"]
