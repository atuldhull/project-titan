"""TITAN-SEC-082 notification wiring: the REAL composition-root budget notifier
(build_budget_notifier) and the deployment-global resume endpoint.

The tier tests exercise BudgetManager with a mock callback; this closes the gap
the Phase-71 review flagged — the actual notifier (which resolves Maintainers and
persists notifications) and the operator resume path were never run.
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.budget_alerts import build_budget_notifier
from titan.llm.budget import GLOBAL_SCOPE_ID, BudgetNotification

from .conftest import run_async

pytestmark = pytest.mark.schema

# The api fixture seeds a user per role with an org-wide grant; owner/admin/
# maintainer are the maintainer tier maintainer_user_ids resolves.
_MAINTAINER_ROLES = ("owner", "admin", "maintainer")


def _notifications_for(engine: AsyncEngine, user_id: uuid.UUID) -> list[Any]:
    async def _q() -> list[Any]:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT category, severity, title, link FROM notifications "
                    "WHERE user_id = :u ORDER BY id"
                ),
                {"u": user_id},
            )
            out: list[Any] = list(rows.all())
            return out

    result: list[Any] = run_async(_q)
    return result


def test_notifier_alerts_every_maintainer_with_the_right_copy(api: dict[str, Any]) -> None:
    engine: AsyncEngine = api["engine"]
    notify = build_budget_notifier(engine)
    maintainers = [api["user_ids"][r] for r in _MAINTAINER_ROLES]
    project = uuid.uuid4()

    # A 100% hold crossing -> a high-severity 'failure' alert to EVERY maintainer.
    run_async(
        lambda: notify(
            BudgetNotification(
                event="hold",
                kind="project",
                scope_id=project,
                cap=Decimal("25"),
                spent=Decimal("25"),
            )
        )
    )
    for uid in maintainers:
        rows = _notifications_for(engine, uid)
        assert len(rows) == 1
        assert rows[0].category == "failure" and rows[0].severity == "high"
        assert rows[0].link == "/monitoring"
    # A non-maintainer (viewer) is NOT alerted.
    assert _notifications_for(engine, api["user_ids"]["viewer"]) == []

    # An 80% crossing -> a low-severity 'activity' heads-up.
    run_async(
        lambda: notify(
            BudgetNotification(
                event="threshold_80",
                kind="org_month",
                scope_id=GLOBAL_SCOPE_ID,
                cap=Decimal("500"),
                spent=Decimal("400"),
            )
        )
    )
    owner_rows = _notifications_for(engine, api["user_ids"]["owner"])
    assert len(owner_rows) == 2
    assert owner_rows[1].category == "activity" and owner_rows[1].severity == "info"


def test_notifier_swallows_a_delivery_failure(api: dict[str, Any], monkeypatch: Any) -> None:
    """A notifier failure must NEVER propagate — it runs on the metering hot path,
    so a DB/identity hiccup is swallowed, not bubbled into a served call. Forcing
    the recipient lookup to throw exercises build_budget_notifier's OWN try/except;
    delete that guard and this test raises (bites)."""
    from titan.identity import service as identity_service

    async def boom(_conn: Any) -> list[uuid.UUID]:
        raise RuntimeError("identity store is down")

    # budget_alerts holds the SAME module object, so patching it here makes its
    # maintainer_user_ids(conn) call throw inside the notifier's try/except.
    monkeypatch.setattr(identity_service, "maintainer_user_ids", boom)
    notify = build_budget_notifier(api["engine"])

    # Must return normally despite the resolver throwing inside the callback.
    run_async(
        lambda: notify(
            BudgetNotification(
                event="hold",
                kind="local_task",
                scope_id=uuid.uuid4(),
                cap=Decimal("1800"),
                spent=Decimal("1800"),
                unit="seconds",
            )
        )
    )
    # The failure was swallowed BEFORE any row was written.
    assert _notifications_for(api["engine"], api["user_ids"]["owner"]) == []


def test_resume_global_budget_endpoint_lifts_the_hold(api: dict[str, Any]) -> None:
    """B5: the operator endpoint clears the current month's org_month hold so the
    deployment resumes. settings:write (owner) required; viewer is forbidden."""
    engine: AsyncEngine = api["engine"]

    async def set_global_hold() -> None:
        async with engine.begin() as conn:
            # Simulate the global monthly ceiling having tripped this month.
            from datetime import UTC, datetime

            from titan.llm.budget import period_scope

            await conn.execute(
                text(
                    "INSERT INTO budget_holds (id, scope, scope_id, reason, threshold_pct) "
                    "VALUES (:id, :s, :sid, 'global cap', 100) ON CONFLICT DO NOTHING"
                ),
                {
                    "id": uuid.uuid4(),
                    "s": period_scope("org_month", datetime.now(UTC)),
                    "sid": GLOBAL_SCOPE_ID,
                },
            )

    run_async(set_global_hold)

    client = api["client"]
    # Viewer lacks settings:write.
    forbidden = client.post(
        "/api/v1/settings/llm/budget:resume-global", headers=api["token_for"]("viewer")
    )
    assert forbidden.status_code == 403

    ok = client.post("/api/v1/settings/llm/budget:resume-global", headers=api["token_for"]("owner"))
    assert ok.status_code == 200 and ok.json()["resumed"] is True

    # The org_month hold is gone -> active_budget_hold no longer pauses tasks.
    async def still_held() -> str | None:
        from titan.llm import service as llm_service

        async with engine.connect() as conn:
            return await llm_service.active_budget_hold(
                conn, project_id=uuid.uuid4(), task_id=uuid.uuid4()
            )

    assert run_async(still_held) is None
