"""Rotation-overdue expiry alerts (Phase 68, TITAN-SEC-052). An overdue secret
raises a bell notification for secret:write holders, deduped across scans."""

import datetime as dt
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.db import uuid7
from titan.vault import alerts, service
from titan.vault.drivers.static import StaticSecretsProvider
from titan.vault.provider import SecretScope

pytestmark = pytest.mark.schema


async def _admin_user(engine: Any) -> uuid.UUID:
    """A user holding the admin role (which grants secret:write)."""
    uid = uuid7()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO roles (id, name, permissions) VALUES (:id, 'admin', :p) "
                "ON CONFLICT (name) DO UPDATE SET permissions = :p"
            ),
            {"id": uuid7(), "p": json.dumps(["secret:read", "secret:write", "*:read"])},
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'Admin', 'local', :s)"
            ),
            {"id": uid, "e": f"alert-admin-{uid.hex[:10]}@titan.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                "SELECT :rid, :uid, r.id, NULL FROM roles r WHERE r.name = 'admin'"
            ),
            {"rid": uuid7(), "uid": uid},
        )
    return uid


def test_overdue_secret_alerts_managers_once(migrated_engine: Any) -> None:
    engine = migrated_engine
    store = StaticSecretsProvider({})

    async def go() -> tuple[int, int, int]:
        admin = await _admin_user(engine)
        name = f"OVERDUE_{uuid7().hex[:8]}"
        async with engine.begin() as conn:
            await service.create_secret(
                conn,
                store,
                name=name,
                value="x",
                scope=SecretScope.ORG,
                scope_ref=None,
                owner_id=None,
                rotation_days=1,
                actor="admin",
            )
            # Backdate the rotation clock so it is overdue NOW.
            await conn.execute(
                text("UPDATE secret_metadata SET rotated_at = :old WHERE name = :n"),
                {"old": dt.datetime.now(dt.UTC) - dt.timedelta(days=30), "n": name},
            )
        first = await alerts.notify_overdue_secrets(engine)
        second = await alerts.notify_overdue_secrets(engine)  # dedup: no new alert
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM notifications "
                        "WHERE user_id = :u AND category = 'security' AND read_at IS NULL"
                    ),
                    {"u": admin},
                )
            ).scalar_one()
        return first, second, count

    first, second, count = run_async(go)
    assert first >= 1  # the admin was alerted
    assert second == 0  # a second scan does not re-alert (deduped)
    assert count >= 1
