"""TITAN-SEC-082 budget-tier extension DoD (Phase 71).

The Phase-17 cumulative task/project caps are tested in ``test_llm_budget``; here
we cover the tiers Phase 71 ADDS on top of the same rolling-counter + budget_hold
machinery:

* per-project/day and global/month WINDOWED caps (period-qualified scope, so a
  new day/month starts fresh and a stale hold is never consulted — rollover
  resumes on its own);
* the DoD path: a global (org/month) breach pauses a task via the EXISTING
  ``active_budget_hold`` gate the scheduler consults, and raising the global
  budget resumes it;
* local models are time-capped, not dollar-capped (a $0 local loop still pauses);
* the 80%/100% notifier fires (in-app + Maintainer alert), deduped per crossing;
* the reservation stays atomic across tiers (a later-tier breach rolls the
  earlier tiers back).

Windowing is made deterministic with an injected clock — never wall-clock.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.llm import service as llm_service
from titan.llm.budget import GLOBAL_SCOPE_ID, BudgetManager, BudgetNotification, spend_key
from titan.llm.config import Budgets

from .conftest import run_async

pytestmark = pytest.mark.schema


def _mgr(
    engine: AsyncEngine,
    *,
    clock: Callable[[], datetime] | None = None,
    notify: Any | None = None,
) -> tuple[BudgetManager, Any]:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return BudgetManager(fake, engine, notify=notify, clock=clock), fake


def _held(
    engine: AsyncEngine,
    mgr: BudgetManager,
    project: uuid.UUID | None,
    task: uuid.UUID | None,
    now: datetime,
) -> str | None:
    async def _q() -> str | None:
        async with engine.connect() as conn:
            return await mgr.held_reason(conn, project, task, now)

    reason: str | None = run_async(_q)
    return reason


@pytest.fixture
def _clean_global(migrated_engine: AsyncEngine) -> Any:
    """The org/month tier is keyed on the GLOBAL singleton, which is shared across
    the disposable DB — scrub its hold before AND after so a global-cap test never
    leaks into (or inherits from) another."""

    async def scrub() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM budget_holds WHERE scope_id = :g"), {"g": GLOBAL_SCOPE_ID}
            )

    run_async(scrub)
    yield
    run_async(scrub)


# --- windowed tiers: per-project/day rollover ------------------------------


def test_project_day_hold_is_per_utc_day(migrated_engine: AsyncEngine) -> None:
    """A daily cap holds the project for THAT UTC day; the next day is a fresh
    scope, so the hold is simply never consulted again (auto rollover)."""
    day1 = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
    day2 = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
    budgets = Budgets(per_project_day_usd=Decimal("0.10"))
    mgr, _fake = _mgr(migrated_engine, clock=lambda: day1)
    project = uuid.uuid4()

    # Settle a cost over the daily cap on day1 -> hold on project_day:2026-07-24.
    run_async(lambda: mgr.settle(budgets, project, None, [], Decimal(0), Decimal("0.15"), day1))

    assert _held(migrated_engine, mgr, project, None, day1) is not None  # paused today
    assert _held(migrated_engine, mgr, project, None, day2) is None  # tomorrow is fresh


def test_reserve_rolls_back_earlier_tier_on_later_breach(migrated_engine: AsyncEngine) -> None:
    """Reservation is atomic across tiers: a project/day breach unwinds the
    cumulative-project increment made moments earlier."""
    now = datetime(2026, 7, 24, tzinfo=UTC)
    budgets = Budgets(per_project_usd=Decimal("100"), per_project_day_usd=Decimal("0.10"))
    mgr, fake = _mgr(migrated_engine, clock=lambda: now)
    project = uuid.uuid4()

    breach, reserved = run_async(lambda: mgr.reserve(budgets, project, None, Decimal("0.20"), now))
    assert breach is not None and breach.kind == "project_day"
    assert reserved == []
    # The cumulative-project counter (checked first, under cap) was rolled back.
    val = run_async(lambda: fake.get(spend_key("project", project)))
    assert val is None or int(val) == 0


# --- the DoD: global (org/month) breach pauses the task, raising resumes -----


def test_org_month_breach_pauses_task_and_raise_resumes(
    _clean_global: Any, migrated_engine: AsyncEngine
) -> None:
    """TITAN-SEC-082 DoD: an org/month breach pauses a task via the SAME
    ``active_budget_hold`` gate the scheduler consults, and raising the global
    budget resumes it (single-org => the global monthly ceiling)."""
    now = datetime.now(UTC)
    budgets = Budgets(per_org_month_usd=Decimal("1.00"))
    mgr, fake = _mgr(migrated_engine, clock=lambda: now)
    project, task = uuid.uuid4(), uuid.uuid4()

    # Breach the global monthly ceiling -> hold on org_month:<this month>, GLOBAL.
    run_async(lambda: mgr.settle(budgets, project, task, [], Decimal(0), Decimal("1.50"), now))

    async def scheduler_view() -> str | None:
        async with migrated_engine.connect() as conn:
            # Exactly the call claim_and_assign / the pipeline make.
            return await llm_service.active_budget_hold(conn, project_id=project, task_id=task)

    assert run_async(scheduler_view) is not None  # the task is paused (budget_hold)

    # Raise the global budget: clears the hold + resets the counter.
    async def raise_global() -> None:
        async with migrated_engine.begin() as conn:
            await llm_service.raise_global_month_budget(conn, redis=fake, now=now)

    run_async(raise_global)
    assert run_async(scheduler_view) is None  # resumed

    # And a fresh reservation now fits (the counter was reset, not just the hold).
    breach, _reserved = run_async(lambda: mgr.reserve(budgets, project, task, Decimal("0.10"), now))
    assert breach is None


# --- local models are time-capped, not dollar-capped ------------------------


def test_local_time_cap_pauses_task_on_free_model(migrated_engine: AsyncEngine) -> None:
    """A runaway LOCAL loop costs $0, so no dollar tier bounds it — cumulative
    local wall-time does. Cloud latency never accrues to this tier."""
    now = datetime.now(UTC)
    budgets = Budgets(local_seconds_per_task=5)  # 5000 ms
    mgr, _fake = _mgr(migrated_engine, clock=lambda: now)
    task = uuid.uuid4()

    # Cloud latency does not count toward the local-time tier.
    run_async(lambda: mgr.settle_local_time(budgets, task, "anthropic", 9000))
    assert _held(migrated_engine, mgr, None, task, now) is None

    # Local latency accrues; the task pauses once it crosses the cap.
    run_async(lambda: mgr.settle_local_time(budgets, task, "ollama", 3000))
    assert _held(migrated_engine, mgr, None, task, now) is None  # 3.0s < 5.0s
    run_async(lambda: mgr.settle_local_time(budgets, task, "ollama", 3000))
    assert _held(migrated_engine, mgr, None, task, now) is not None  # 6.0s >= 5.0s -> held


# --- 80% / 100% notifications (in-app + Maintainer alert) -------------------


def test_notifier_fires_at_80_then_100_deduped(migrated_engine: AsyncEngine) -> None:
    events: list[tuple[str, str]] = []

    async def notify(note: BudgetNotification) -> None:
        events.append((note.event, note.kind))

    now = datetime.now(UTC)
    budgets = Budgets(per_task_usd=Decimal("1.00"))
    mgr, _fake = _mgr(migrated_engine, clock=lambda: now, notify=notify)
    task = uuid.uuid4()

    # 0.85 -> crosses 80%; a second 80%-band settle must NOT re-notify (deduped).
    run_async(lambda: mgr.settle(budgets, None, task, [], Decimal(0), Decimal("0.85"), now))
    run_async(lambda: mgr.settle(budgets, None, task, [], Decimal(0), Decimal("0.05"), now))
    assert events.count(("threshold_80", "task")) == 1

    # Cross 100% -> a hold notification (the Maintainer alert). A SECOND at-cap
    # settle (as concurrent in-flight calls do) must NOT re-fire it (deduped).
    run_async(lambda: mgr.settle(budgets, None, task, [], Decimal(0), Decimal("0.20"), now))
    run_async(lambda: mgr.settle(budgets, None, task, [], Decimal(0), Decimal("0.20"), now))
    assert events.count(("hold", "task")) == 1


def test_notifier_failure_never_propagates(migrated_engine: AsyncEngine) -> None:
    """A notification hiccup must never fail the metering of a served call."""

    async def boom(_note: BudgetNotification) -> None:
        raise RuntimeError("pager is down")

    now = datetime.now(UTC)
    budgets = Budgets(per_task_usd=Decimal("0.10"))
    mgr, _fake = _mgr(migrated_engine, clock=lambda: now, notify=boom)
    task = uuid.uuid4()
    # Settle over the cap -> _enforce fires the (throwing) notifier; settle swallows.
    run_async(lambda: mgr.settle(budgets, None, task, [], Decimal(0), Decimal("0.20"), now))
    # The hold was still persisted despite the notifier throwing.
    assert _held(migrated_engine, mgr, None, task, now) is not None
