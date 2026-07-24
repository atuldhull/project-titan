"""Layered LLM budget enforcement (TITAN-SEC-082, TITAN-OPS-021).

Redis holds cumulative spend counters in micro-dollars (integer-exact). The gate
is ATOMIC with the spend it guards: the pre-request estimate is *reserved* with a
single ``INCRBY`` and the returned total is compared to the cap, so concurrent
calls cannot both slip under it. The reservation is reconciled to the actual cost
at settle time (or released if the call never happened). At 100% a persisted
``budget_hold`` hard-stops further calls; at 80% a once-per-crossing notification
fires (deduped via a Redis flag). A per-project remaining gauge feeds the
LLM-spend dashboard (Vol 9 §10).

The TITAN-SEC-082 tier set (Phase 71 extends the Phase-18 cumulative caps):

    task         cumulative per task            per_task_usd            $
    project      cumulative per project         per_project_usd         $
    project_day  per project, per UTC day       per_project_day_usd     $  windowed
    org_month    deployment-global, per month   per_org_month_usd       $  windowed
    local_task   cumulative LOCAL wall-seconds  local_seconds_per_task  s  metered post-call

TITAN is single-org by construction (no ``organizations`` table — see
``identity.accounts.list_members``), so Vol 9 §10's "global monthly ceiling →
per-organization budget" collapse into ONE deployment-wide monthly cap, keyed on
the :data:`GLOBAL_SCOPE_ID` singleton. Local models cost nothing, so a dollar cap
can never bound a runaway LOCAL loop; the ``local_task`` tier time-caps them.

Windowed tiers bake the period (``YYYY-MM-DD`` / ``YYYY-MM``) into the Redis key
AND the persisted hold scope, so a new day/month starts fresh with no bookkeeping
and a stale hold is simply never consulted again — the resume is automatic at
rollover. The counters carry a TTL so old windows self-clean.

Injecting ``redis=None``/``engine=None`` disables budgets (unit tests, zero-config
local) — the gateway then never blocks on spend.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

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

#: The singleton scope for the deployment-global monthly ceiling. TITAN is
#: single-org, so "the org" is the whole deployment; the nil UUID names it.
GLOBAL_SCOPE_ID = uuid.UUID(int=0)

#: Providers whose calls are $0 and therefore time-capped, not dollar-capped.
LOCAL_PROVIDERS = frozenset({"ollama"})

# Windowed-counter retention: long enough to outlive the window + reconciliation,
# short enough that yesterday's / last month's keys evict themselves.
_DAY_TTL_S = 60 * 60 * 48
_MONTH_TTL_S = 60 * 60 * 24 * 40

# A scope reservation: (scope_string, scope_id) whose counter carries a live
# estimate. The scope string is period-qualified for windowed tiers.
Reserved = list[tuple[str, uuid.UUID]]

Clock = Callable[[], datetime]


def spend_key(scope: str, scope_id: uuid.UUID) -> str:
    """The Redis rolling-spend counter key for a budget scope. Shared so that
    raising a budget (clearing the hold) can also reset this counter — otherwise
    a stale at-cap counter would make the next reservation re-breach and re-hold.
    For windowed tiers ``scope`` is period-qualified (``project_day:2026-07-24``),
    so the key rolls over on its own."""
    return f"llm:spend:{scope}:{scope_id}"


def period_scope(kind: str, now: datetime) -> str:
    """Period-qualify a tier ``kind`` for the given instant. Cumulative tiers
    (task/project/local_task) are returned unchanged; windowed tiers gain their
    period suffix so day/month counters and holds are naturally scoped."""
    if kind == "project_day":
        return f"project_day:{now.strftime('%Y-%m-%d')}"
    if kind == "org_month":
        return f"org_month:{now.strftime('%Y-%m')}"
    return kind


def ttl_for_kind(kind: str) -> int | None:
    """Counter/flag retention for a tier: windowed tiers self-expire with their
    period; cumulative tiers persist until raised (None)."""
    if kind == "project_day":
        return _DAY_TTL_S
    if kind == "org_month":
        return _MONTH_TTL_S
    return None


def hold_candidates(
    project_id: uuid.UUID | None, task_id: uuid.UUID | None, now: datetime
) -> list[tuple[str, uuid.UUID]]:
    """Every (scope, scope_id) pair whose persisted hold would pause this call,
    for the CURRENT period. A hold only exists if a cap was configured and
    breached, so listing all tiers is safe — absent holds don't match. Shared by
    the gateway pre-call gate and the ``active_budget_hold`` facade the scheduler
    and pipeline consult."""
    candidates: list[tuple[str, uuid.UUID]] = []
    if task_id is not None:
        candidates.append(("task", task_id))
        candidates.append(("local_task", task_id))
    if project_id is not None:
        candidates.append(("project", project_id))
        candidates.append((period_scope("project_day", now), project_id))
    candidates.append((period_scope("org_month", now), GLOBAL_SCOPE_ID))
    return candidates


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
class Cap:
    """One applicable budget tier for a call: its stable ``kind`` label, the
    period-qualified ``scope`` used for the Redis key + hold, the entity id, the
    dollar cap, and (for windowed tiers) the counter TTL."""

    kind: str  # 'task' | 'project' | 'project_day' | 'org_month'
    scope: str
    scope_id: uuid.UUID
    cap: Decimal
    ttl_s: int | None = None


@dataclass(frozen=True)
class Breach:
    """A cap the reservation could not fit under."""

    kind: str  # 'task' | 'project' | 'project_day' | 'org_month'
    scope: str
    scope_id: uuid.UUID
    cap: Decimal


@dataclass(frozen=True)
class BudgetNotification:
    """A threshold crossing routed to the composition-root notifier (in-app +
    ticket at 80%, Maintainer alert at 100%). Carries references only — never
    prompt/output — per TITAN-OPS-016."""

    event: Literal["threshold_80", "hold"]
    kind: str
    scope_id: uuid.UUID
    cap: Decimal
    spent: Decimal
    unit: Literal["usd", "seconds"] = "usd"


Notifier = Callable[[BudgetNotification], Awaitable[None]]


class BudgetManager:
    """Atomic rolling-counter budget gate. All methods no-op when Redis is absent.

    ``notify`` is a composition-root callback (wired in ``main``/the worker) so
    the ``llm`` module never imports the notification sink directly — the same
    boundary-preserving pattern as the Phase-70 audit hooks. ``clock`` is
    injectable so windowed tiers are deterministic under test."""

    def __init__(
        self,
        redis: Redis | None,
        engine: AsyncEngine | None,
        *,
        notify: Notifier | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._redis = redis
        self._engine = engine
        self._notify_cb = notify
        self._clock: Clock = clock or (lambda: datetime.now(UTC))

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    def now(self) -> datetime:
        """The manager's clock — captured once per gateway call so a call that
        straddles a UTC midnight/month boundary reserves and settles against the
        SAME window (no double-count at the seam)."""
        return self._clock()

    @staticmethod
    def _caps(
        budgets: Budgets | None,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        now: datetime,
    ) -> list[Cap]:
        """The dollar-denominated tiers applicable to this call. The local-time
        tier is metered separately (post-call, in :meth:`settle_local_time`)."""
        if budgets is None:
            return []
        out: list[Cap] = []
        if budgets.per_task_usd is not None and task_id is not None:
            out.append(Cap("task", "task", task_id, budgets.per_task_usd))
        if budgets.per_project_usd is not None and project_id is not None:
            out.append(Cap("project", "project", project_id, budgets.per_project_usd))
        if budgets.per_project_day_usd is not None and project_id is not None:
            out.append(
                Cap(
                    "project_day",
                    period_scope("project_day", now),
                    project_id,
                    budgets.per_project_day_usd,
                    _DAY_TTL_S,
                )
            )
        if budgets.per_org_month_usd is not None:
            out.append(
                Cap(
                    "org_month",
                    period_scope("org_month", now),
                    GLOBAL_SCOPE_ID,
                    budgets.per_org_month_usd,
                    _MONTH_TTL_S,
                )
            )
        return out

    async def held_reason(
        self,
        conn: AsyncConnection,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        now: datetime | None = None,
    ) -> str | None:
        """The reason a project/task is on budget hold in the current period, else
        None — across every tier (task, project, project/day, global/month,
        local-time)."""
        candidates = hold_candidates(project_id, task_id, now or self.now())
        row = await repo.get_hold(conn, candidates)
        return row.reason if row is not None else None

    async def reserve(
        self,
        budgets: Budgets | None,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        estimated_cost: Decimal,
        now: datetime,
    ) -> tuple[Breach | None, Reserved]:
        """Atomically reserve ``estimated_cost`` against every applicable cap. On
        breach, any partial reservation is rolled back and ``[]`` is returned; on
        success the reserved scopes are returned for later settle/release."""
        if not self.enabled or budgets is None:
            return None, []
        assert self._redis is not None
        micro = int(estimated_cost * _MICRO)
        reserved: Reserved = []
        for cap in self._caps(budgets, project_id, task_id, now):
            key = spend_key(cap.scope, cap.scope_id)
            new = await self._redis.incrby(key, micro)
            if cap.ttl_s is not None:
                await self._redis.expire(key, cap.ttl_s)  # keep windowed keys self-cleaning
            if new > int(cap.cap * _MICRO):
                await self._redis.decrby(key, micro)  # release self
                await self.release(reserved, estimated_cost)  # release earlier scopes
                return Breach(cap.kind, cap.scope, cap.scope_id, cap.cap), []
            reserved.append((cap.scope, cap.scope_id))
        return None, reserved

    async def persist_hold(self, conn: AsyncConnection, breach: Breach, reason: str) -> None:
        """Write the pre-call hard-stop hold. The Maintainer notification is NOT
        fired here — the caller dispatches it via :meth:`notify_hold_once` AFTER
        this transaction closes, so the notifier never opens a second pooled
        connection while this one is still checked out (review: pool-starvation)."""
        await repo.set_hold(conn, scope=breach.scope, scope_id=breach.scope_id, reason=reason)

    async def notify_hold_once(self, cap: Cap, spent: Decimal, *, unit: str = "usd") -> None:
        """Fire the 100% 'hold' Maintainer alert AT MOST ONCE per crossing (Redis
        SETNX), so the herd of in-flight calls crossing a cap at the same instant
        does not fan out duplicate high-severity alerts + N inserts each. The flag
        inherits the tier's window TTL and raise_budget clears it. Call OUTSIDE any
        open DB transaction — the notifier opens its own connection.

        Best-effort like all metering: a Redis hiccup here is swallowed so it never
        MASKS the caller's real signal (the pre-call breach path must raise its
        402, not a bubbled-up ConnectionError — review)."""
        if not self.enabled:
            return
        assert self._redis is not None
        try:
            flag = f"{spend_key(cap.scope, cap.scope_id)}:notified100"
            if await self._redis.set(flag, "1", nx=True, ex=cap.ttl_s):
                await self._notify("hold", cap, spent, unit=unit)
        except Exception as exc:  # noqa: BLE001 — an alert hiccup never fails the call
            logger.warning("budget.hold_notify_failed", kind=cap.kind, error=str(exc))

    async def release(self, reserved: Reserved, estimated_cost: Decimal) -> None:
        """Undo a reservation for a call that never happened (dispatch failed)."""
        if not self.enabled or not reserved:
            return
        assert self._redis is not None
        micro = int(estimated_cost * _MICRO)
        for scope, scope_id in reserved:
            await self._redis.decrby(spend_key(scope, scope_id), micro)

    async def settle(
        self,
        budgets: Budgets | None,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
        reserved: Reserved,
        estimated_cost: Decimal,
        actual_cost: Decimal,
        now: datetime,
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
        # Each tier is metered/enforced in its OWN try/except so one tier's Redis
        # or hold-write failure cannot skip the LATER tiers — notably the global
        # org_month backstop, which _caps orders last (review: metering-integrity).
        for cap in self._caps(budgets, project_id, task_id, now):
            try:
                delta = (
                    act_micro - est_micro
                    if (cap.scope, cap.scope_id) in reserved_set
                    else act_micro
                )
                key = spend_key(cap.scope, cap.scope_id)
                total = await self._redis.incrby(key, delta)
                if cap.ttl_s is not None:
                    await self._redis.expire(key, cap.ttl_s)
                await self._enforce(cap, Decimal(total) / _MICRO)
            except Exception as exc:  # noqa: BLE001 — metering must never fail the call
                logger.warning("budget.settle_tier_failed", kind=cap.kind, error=str(exc))

    async def settle_local_time(
        self,
        budgets: Budgets | None,
        task_id: uuid.UUID | None,
        provider: str,
        latency_ms: int,
    ) -> None:
        """Meter a LOCAL call's wall time toward the per-task time cap (§ SEC-082
        "local models time-capped"). Post-call, best-effort — a runaway local loop
        pauses the task within one iteration of crossing the cap."""
        if not self.enabled or budgets is None or budgets.local_seconds_per_task is None:
            return
        if task_id is None or provider not in LOCAL_PROVIDERS or latency_ms <= 0:
            return
        assert self._redis is not None
        key = spend_key("local_task", task_id)
        cap_ms = budgets.local_seconds_per_task * 1000
        cap = Cap("local_task", "local_task", task_id, Decimal(budgets.local_seconds_per_task))
        try:
            total_ms = await self._redis.incrby(key, latency_ms)
            spent_s = Decimal(total_ms) / 1000
            if total_ms >= cap_ms and self._engine is not None:
                async with self._engine.begin() as conn:
                    await repo.set_hold(
                        conn,
                        scope="local_task",
                        scope_id=task_id,
                        reason=f"local model time cap reached ({budgets.local_seconds_per_task}s)",
                    )
                logger.warning("budget.local_hold_set", task_id=str(task_id), cap_s=cap.cap)
                await self.notify_hold_once(cap, spent_s, unit="seconds")
            elif Decimal(total_ms) >= _NOTIFY_PCT * cap_ms:
                # local_task is a CUMULATIVE tier (no window), so the 80% flag has
                # no TTL and is cleared on raise — like the other cumulative tiers.
                if await self._redis.set(f"{key}:notified80", "1", nx=True):
                    await self._notify("threshold_80", cap, spent_s, unit="seconds")
        except Exception as exc:  # noqa: BLE001 — metering must never fail the call
            logger.warning("budget.local_settle_failed", error=str(exc))

    async def _enforce(self, cap: Cap, spent: Decimal) -> None:
        assert self._redis is not None
        if cap.kind == "project":
            metrics.PROJECT_BUDGET_REMAINING_USD.labels(str(cap.scope_id)).set(
                float(max(Decimal(0), cap.cap - spent))
            )
        if spent >= cap.cap and self._engine is not None:
            async with self._engine.begin() as conn:
                await repo.set_hold(
                    conn,
                    scope=cap.scope,
                    scope_id=cap.scope_id,
                    reason=f"{cap.kind} budget reached ({cap.cap})",
                )
            logger.warning(
                "budget.hold_set", kind=cap.kind, scope=cap.scope, scope_id=str(cap.scope_id)
            )
            await self.notify_hold_once(cap, spent)
        elif spent >= cap.cap * _NOTIFY_PCT:
            # Fire the 80% notification once per crossing (Redis SETNX flag). The
            # flag inherits the window TTL so a new period re-notifies.
            flag = f"{spend_key(cap.scope, cap.scope_id)}:notified80"
            if await self._redis.set(flag, "1", nx=True, ex=cap.ttl_s):
                logger.info("budget.threshold_80", kind=cap.kind, scope=cap.scope, spent=str(spent))
                await self._notify("threshold_80", cap, spent)

    async def _notify(
        self, event: Literal["threshold_80", "hold"], cap: Cap, spent: Decimal, *, unit: str = "usd"
    ) -> None:
        if self._notify_cb is None:
            return
        note = BudgetNotification(
            event=event,
            kind=cap.kind,
            scope_id=cap.scope_id,
            cap=cap.cap,
            spent=spent,
            unit="seconds" if unit == "seconds" else "usd",
        )
        try:
            await self._notify_cb(note)
        except Exception:  # noqa: BLE001 — a notification failure never fails the call
            # 'crossing=', never 'event=' — 'event' is structlog's positional log
            # name, so 'event=' would raise HERE and defeat the swallow.
            logger.warning("budget.notify_failed", crossing=event, kind=cap.kind, exc_info=True)


__all__ = [
    "GLOBAL_SCOPE_ID",
    "LOCAL_PROVIDERS",
    "Breach",
    "BudgetManager",
    "BudgetNotification",
    "Cap",
    "Notifier",
    "Reserved",
    "estimate_input_tokens",
    "hold_candidates",
    "period_scope",
    "spend_key",
    "ttl_for_kind",
]
