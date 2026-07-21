"""Vault service layer (Phase 68) — metadata + audit + injection/envelope audit,
against the test DB with an in-memory store standing in for the age vault."""

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.vault import service
from titan.vault.drivers.static import StaticSecretsProvider
from titan.vault.provider import SecretScope

pytestmark = pytest.mark.schema


def test_create_rotate_delete_updates_store_and_audits(migrated_engine: Any) -> None:
    store = StaticSecretsProvider({})

    async def go() -> dict[str, Any]:
        async with migrated_engine.begin() as conn:
            await service.create_secret(
                conn,
                store,
                name="API_KEY",
                value="v1",
                scope=SecretScope.ORG,
                scope_ref=None,
                owner_id=None,
                rotation_days=90,
                actor="admin",
            )
        v1 = await store.get("API_KEY")
        async with migrated_engine.begin() as conn:
            await service.rotate_secret(conn, store, name="API_KEY", value="v2", actor="admin")
        v2 = await store.get("API_KEY")
        async with migrated_engine.begin() as conn:
            metas = await service.list_secret_metadata(conn)
        async with migrated_engine.begin() as conn:
            await service.delete_secret(conn, store, name="API_KEY", actor="admin")
        names_after = await store.names()
        async with migrated_engine.connect() as conn:
            events = [
                r.event_type
                for r in (
                    await conn.execute(
                        text(
                            "SELECT event_type FROM secret_audit "
                            "WHERE secret_name = 'API_KEY' ORDER BY created_at"
                        )
                    )
                ).all()
            ]
        return {
            "v1": v1,
            "v2": v2,
            "metas": [m.name for m in metas],
            "after": names_after,
            "events": events,
        }

    r = run_async(go)
    assert r["v1"] == "v1" and r["v2"] == "v2"  # store follows create/rotate
    assert "API_KEY" in r["metas"]
    assert r["after"] == []  # delete removed the value too
    assert r["events"] == ["secret.created", "secret.rotated", "secret.deleted"]


def test_create_duplicate_rolls_back_without_touching_store(migrated_engine: Any) -> None:
    store = StaticSecretsProvider({})

    async def go() -> tuple[bool, list[str]]:
        async with migrated_engine.begin() as conn:
            await service.create_secret(
                conn,
                store,
                name="DUP",
                value="first",
                scope=SecretScope.ORG,
                scope_ref=None,
                owner_id=None,
                rotation_days=90,
                actor="a",
            )
        raised = False
        try:
            async with migrated_engine.begin() as conn:
                await service.create_secret(
                    conn,
                    store,
                    name="DUP",
                    value="second",
                    scope=SecretScope.ORG,
                    scope_ref=None,
                    owner_id=None,
                    rotation_days=90,
                    actor="a",
                )
        except service.SecretExists:
            raised = True
        return raised, [await store.get("DUP")]

    raised, stored = run_async(go)
    assert raised is True
    assert stored == ["first"]  # the store still holds the ORIGINAL value


def test_invalid_name_is_rejected_before_any_write(migrated_engine: Any) -> None:
    store = StaticSecretsProvider({})

    async def go() -> list[str]:
        for bad in ("../etc", "a/b", "has space", ".."):
            with pytest.raises(service.InvalidSecretName):
                async with migrated_engine.begin() as conn:
                    await service.create_secret(
                        conn,
                        store,
                        name=bad,
                        value="x",
                        scope=SecretScope.ORG,
                        scope_ref=None,
                        owner_id=None,
                        rotation_days=90,
                        actor="a",
                    )
        return await store.names()

    assert run_async(go) == []


def test_overdue_detection(migrated_engine: Any) -> None:
    store = StaticSecretsProvider({})

    async def go() -> list[str]:
        async with migrated_engine.begin() as conn:
            await service.create_secret(
                conn,
                store,
                name="ROTATE_ME",
                value="x",
                scope=SecretScope.ORG,
                scope_ref=None,
                owner_id=None,
                rotation_days=90,
                actor="a",
            )
        # 100 days later, the 90-day window has elapsed.
        future = dt.datetime.now(dt.UTC) + dt.timedelta(days=100)
        async with migrated_engine.connect() as conn:
            overdue = await service.overdue_secrets(conn, now=future)
        return [m.name for m in overdue]

    assert "ROTATE_ME" in run_async(go)


def test_envelope_rejection_audit_has_digest_not_value(migrated_engine: Any) -> None:
    async def go() -> dict[str, Any]:
        await service.record_envelope_rejection(
            migrated_engine,
            actor="attacker",
            topic="stream:events.domain",
            reason="bad_signature",
            event_id="e1",
            payload_digest="abc123",
        )
        async with migrated_engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT event_type, detail FROM secret_audit "
                        "WHERE event_type = 'envelope.rejected' ORDER BY created_at DESC LIMIT 1"
                    )
                )
            ).one()
        return {"type": row.event_type, "detail": row.detail}

    r = run_async(go)
    assert r["type"] == "envelope.rejected"
    assert r["detail"]["reason"] == "bad_signature"
    assert r["detail"]["payload_sha256"] == "abc123"
