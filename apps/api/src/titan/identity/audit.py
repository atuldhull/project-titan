"""Security audit events (Phase 67; TITAN-SEC-017, Vol 8 §10.2).

A thin append-only writer over the ``security_events`` table. It lives inside
``titan.identity`` on purpose: the module-independence contract forbids identity
from importing the project-scoped ``tool_audit`` ledger, and these events are
org/user-scoped rather than project-scoped anyway — a failed login has no
project.

Every write is best-effort from the caller's point of view but transactional
here: audit is a record of what happened, and losing an audit row must never be
able to fail the request that produced it. Callers that already hold a
transaction pass their connection; callers on the login path (which must record
a failure even as they raise) use :func:`record` with the engine so the row
commits independently of the 401.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from titan.db import uuid7

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class SecurityEventType(StrEnum):
    """The auth events worth a durable record. Values are the stored strings."""

    LOGIN_SUCCEEDED = "login.succeeded"
    LOGIN_FAILED = "login.failed"
    LOGIN_THROTTLED = "login.throttled"
    MFA_ENROLLED = "mfa.enrolled"
    MFA_VERIFIED = "mfa.verified"
    MFA_FAILED = "mfa.failed"
    MFA_RECOVERY_USED = "mfa.recovery_used"
    MFA_REQUIRED_BLOCKED = "mfa.required_blocked"
    MFA_POLICY_CHANGED = "mfa.policy_changed"


_INSERT = text(
    "INSERT INTO security_events (id, event_type, user_id, email, source_ip, detail) "
    "VALUES (:id, :event_type, :user_id, :email, :source_ip, CAST(:detail AS jsonb))"
)


async def write(
    conn: AsyncConnection,
    event_type: SecurityEventType,
    *,
    user_id: uuid.UUID | None = None,
    email: str | None = None,
    source_ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one event within the caller's transaction."""
    import json

    await conn.execute(
        _INSERT,
        {
            "id": uuid7(),
            "event_type": str(event_type),
            "user_id": user_id,
            "email": email,
            "source_ip": source_ip,
            "detail": json.dumps(detail or {}),
        },
    )


async def record(
    engine: AsyncEngine,
    event_type: SecurityEventType,
    *,
    user_id: uuid.UUID | None = None,
    email: str | None = None,
    source_ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one event in its own transaction.

    For the login path: a failed-login event must commit even though the
    request goes on to raise 401, so it cannot ride the request transaction.
    """
    async with engine.begin() as conn:
        await write(
            conn,
            event_type,
            user_id=user_id,
            email=email,
            source_ip=source_ip,
            detail=detail,
        )
