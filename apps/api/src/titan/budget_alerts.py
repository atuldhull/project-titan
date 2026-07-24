"""Composition-root wiring for TITAN-SEC-082 budget-threshold notifications.

The ``llm`` budget manager fires a :class:`~titan.llm.budget.BudgetNotification`
at the 80% and 100% crossings but must NOT import ``messaging``/``identity`` (the
module-independence contract). This root-level helper — imported only by the
process entrypoints (``main``/``worker``/``cli``), never by a domain module —
builds the callback that resolves Maintainers and raises an in-app notification,
then injects it into the gateway via ``build_gateway(budget_notifier=...)``.

At 80%: an in-app notification to every Maintainer (the "in-app + ticket" alert).
At 100%: a high-severity notification that a cap was reached and the task paused
into ``budget_hold`` — a Maintainer raises the budget rather than output being
silently truncated (Vol 9 §10).
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.identity import service as identity_service
from titan.llm.service import BudgetNotification, Notifier
from titan.messaging import service as messaging_service

logger = structlog.get_logger("titan.budget_alerts")

_SCOPE_LABEL = {
    "task": "task",
    "project": "project",
    "project_day": "project (today)",
    "org_month": "organization (this month)",
    "local_task": "task (local-model time)",
}


def _copy(note: BudgetNotification) -> tuple[str, str, str, str]:
    """(category, severity, title, body) for a threshold crossing. Never carries
    prompt/output text — references only (TITAN-OPS-016)."""
    where = _SCOPE_LABEL.get(note.kind, note.kind)
    if note.unit == "seconds":
        spent, cap = f"{note.spent:.0f}s", f"{note.cap:.0f}s"
    else:
        spent, cap = f"${note.spent:.2f}", f"${note.cap:.2f}"
    if note.event == "hold":
        return (
            "failure",
            "high",
            f"LLM budget cap reached for {where}",
            f"Spend {spent} reached the {cap} cap. New LLM calls are paused on a budget "
            "hold; raise the budget to resume the affected work.",
        )
    return (
        "activity",
        "info",
        f"LLM budget at 80% for {where}",
        f"Spend {spent} of the {cap} cap. Work continues; raise the budget to avoid a pause.",
    )


def build_budget_notifier(engine: AsyncEngine) -> Notifier:
    """A :data:`~titan.llm.budget.Notifier` that alerts Maintainers on budget
    crossings. Self-contained and exception-safe: a delivery failure is logged,
    never raised (the budget manager also guards the call), so metering a served
    LLM call never fails on a notification hiccup."""

    async def _notify(note: BudgetNotification) -> None:
        category, severity, title, body = _copy(note)
        try:
            async with engine.begin() as conn:
                recipients = await identity_service.maintainer_user_ids(conn)
                for user_id in recipients:
                    await messaging_service.create_notification(
                        conn,
                        user_id=user_id,
                        category=category,
                        severity=severity,
                        title=title,
                        body=body,
                        link="/monitoring",
                        actor="budget",
                    )
        except Exception:  # noqa: BLE001 — an alert failure never fails the call
            # NB: 'crossing=', not 'event=' — structlog reserves 'event' for the
            # positional log name, so 'event=' would raise inside this very handler
            # and defeat the swallow (the fail-safe must not fault).
            logger.warning(
                "budget.notify_dispatch_failed", crossing=note.event, kind=note.kind, exc_info=True
            )

    return _notify


__all__ = ["build_budget_notifier"]
