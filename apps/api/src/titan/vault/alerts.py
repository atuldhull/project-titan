"""Rotation-overdue expiry alerts (TITAN-SEC-052).

A secret past its rotation window raises a dashboard alert — delivered as a
``security``-category notification (Vol 4 §7.1) to the people who can act on it:
the holders of ``secret:write`` (Owner/Admin/Maintainer). Deduped so a periodic
scan does not re-alert an already-flagged secret every pass; a rotation clears
the overdue state and the next real overdue re-alerts.

Reuses ``messaging.create_notification`` (the one bell-badge seam) and the RBAC
reverse lookup — no parallel alerting path.
"""

from __future__ import annotations

import datetime as dt
import uuid

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.gateway.rbac import granting_permissions
from titan.messaging.service import create_notification
from titan.vault.service import overdue_secrets

logger = structlog.get_logger("titan.vault.alerts")

_LINK_PREFIX = "/settings/secrets#"


def _link_for(name: str) -> str:
    return f"{_LINK_PREFIX}{name}"


async def _secret_managers(conn: AsyncConnection) -> list[uuid.UUID]:
    """User ids that hold ``secret:write`` — the reverse-lookup drives recipients
    off the SAME wildcard rules as the forward RBAC check (Owner via ``*``, Admin
    and Maintainer via the explicit grant)."""
    grants = sorted(granting_permissions("secret:write"))
    rows = (
        await conn.execute(
            text(
                "SELECT DISTINCT u.id FROM users u "
                "JOIN user_roles ur ON ur.user_id = u.id "
                "JOIN roles r ON r.id = ur.role_id "
                "WHERE r.permissions ?| :grants"
            ),
            {"grants": grants},
        )
    ).all()
    return [row.id for row in rows]


async def _already_alerted(conn: AsyncConnection, user_id: uuid.UUID, link: str) -> bool:
    """True if an UNREAD overdue alert for this secret already sits in the user's
    bell — the dedup that stops a periodic scan from re-notifying."""
    row = (
        await conn.execute(
            text(
                "SELECT 1 FROM notifications "
                "WHERE user_id = :uid AND link = :link AND read_at IS NULL LIMIT 1"
            ),
            {"uid": user_id, "link": link},
        )
    ).first()
    return row is not None


async def notify_overdue_secrets(engine: AsyncEngine, *, now: dt.datetime | None = None) -> int:
    """Alert secret managers about every overdue secret (deduped). Returns the
    number of notifications created."""
    now = now or dt.datetime.now(dt.UTC)
    created = 0
    async with engine.begin() as conn:
        overdue = await overdue_secrets(conn, now=now)
        if not overdue:
            return 0
        recipients = await _secret_managers(conn)
        for meta in overdue:
            link = _link_for(meta.name)
            for user_id in recipients:
                if await _already_alerted(conn, user_id, link):
                    continue
                await create_notification(
                    conn,
                    user_id=user_id,
                    category="security",
                    severity="warning",
                    title="Secret rotation overdue",
                    body=(
                        f"Secret '{meta.name}' passed its {meta.rotation_days}-day "
                        "rotation window. Rotate it to clear this alert."
                    ),
                    link=link,
                )
                created += 1
    if created:
        logger.info("vault.overdue_alerts", created=created, overdue=len(overdue))
    return created


__all__ = ["notify_overdue_secrets"]
