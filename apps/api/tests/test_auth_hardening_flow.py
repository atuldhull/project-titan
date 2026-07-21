"""Login throttling + security audit, end to end (Phase 67; TITAN-SEC-017).

Drives the real `/api/v1/auth/token` password path against Postgres + fakeredis,
so it exercises the throttle wiring, the 429 + Retry-After, and the
`security_events` rows the throttle and login emit.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import run_async
from tests.oauth_helpers import authorize, login
from titan import clients
from titan.db import uuid7
from titan.identity import throttle
from titan.identity.service import set_local_password
from titan.main import create_app
from titan.settings import Settings

pytestmark = pytest.mark.schema

EMAIL = "hardening-admin@titan.local"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(
    migrated_engine: Any, schema_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(clients, "create_redis", lambda _settings: fake)

    async def reset() -> None:
        # Per-test isolation over the session-scoped engine: the audit-row counts
        # below assume a clean `security_events`, and a leaked user/login from a
        # neighbouring test would make them flaky.
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    # org_security_policy and the mfa tables MUST be here too: a
                    # neighbouring suite that turned `mfa_required` on would make
                    # this owner hit forced-enrollment and never complete a
                    # login — which is exactly how this failed in-suite while
                    # passing alone.
                    "TRUNCATE org_security_policy, mfa_recovery_codes, "
                    "mfa_credentials, security_events, refresh_tokens, "
                    "user_roles, local_credentials, users RESTART IDENTITY CASCADE"
                )
            )

    run_async(reset)

    async def ensure_roles() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO roles (id, name, permissions) "
                    "VALUES (:id, 'owner', '[\"*\"]'::jsonb) "
                    "ON CONFLICT (name) DO NOTHING"
                ),
                {"id": uuid7()},
            )

    async def seed_user() -> None:
        async with migrated_engine.begin() as conn:
            uid = uuid7()
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :email, 'Hardening Admin', 'local', :subj) "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"id": uid, "email": EMAIL, "subj": str(uid)},
            )
            row = await conn.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": EMAIL}
            )
            uid = row.scalar_one()
            await set_local_password(conn, uid, PASSWORD)
            await conn.execute(
                text(
                    "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                    "SELECT :rid, :uid, r.id, NULL FROM roles r WHERE r.name = 'owner' "
                    "ON CONFLICT (user_id, role_id, project_id) DO NOTHING"
                ),
                {"rid": uuid7(), "uid": uid},
            )

    run_async(ensure_roles)
    run_async(seed_user)

    settings = Settings(database_url=schema_url, otel_exporter_otlp_endpoint="", _env_file=None)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def _events(migrated_engine: Any, event_type: str) -> int:
    async def count() -> int:
        async with migrated_engine.begin() as conn:
            row = await conn.execute(
                text("SELECT count(*) FROM security_events WHERE event_type = :t AND email = :e"),
                {"t": event_type, "e": EMAIL},
            )
            return int(row.scalar_one())

    return int(run_async(count))


def test_successful_login_writes_an_audit_event(client: TestClient, migrated_engine: Any) -> None:
    login(client, EMAIL, PASSWORD)
    assert _events(migrated_engine, "login.succeeded") == 1


def test_five_failures_then_throttled_with_retry_after(
    client: TestClient, migrated_engine: Any
) -> None:
    for _ in range(throttle.THRESHOLD):
        bad = authorize(client, EMAIL, "wrong")
        assert bad.status_code == 401

    # The 6th attempt is refused BEFORE the password is checked.
    throttled = authorize(client, EMAIL, "wrong")
    assert throttled.status_code == 429
    assert int(throttled.headers["Retry-After"]) >= 1
    # The message must not confirm the account exists.
    assert "lock" not in throttled.text.lower()

    assert _events(migrated_engine, "login.failed") == throttle.THRESHOLD
    assert _events(migrated_engine, "login.throttled") >= 1


def test_correct_password_is_still_throttled_within_backoff(
    client: TestClient,
) -> None:
    """Throttle is checked before verify, so even the right password waits."""
    for _ in range(throttle.THRESHOLD):
        authorize(client, EMAIL, "wrong")
    blocked = authorize(client, EMAIL, PASSWORD)
    assert blocked.status_code == 429


def test_a_nonexistent_account_is_throttled_identically(
    client: TestClient,
) -> None:
    """Enumeration resistance: an unknown email throttles like a real one."""
    ghost = "does-not-exist@titan.local"
    for _ in range(throttle.THRESHOLD):
        r = authorize(client, ghost, "wrong")
        assert r.status_code == 401
    throttled = authorize(client, ghost, "wrong")
    assert throttled.status_code == 429


def test_password_grant_is_rejected_with_an_oauth_error(client: TestClient) -> None:
    """SEC-010: the OAuth password grant is disabled at the endpoint."""
    r = client.post(
        "/api/v1/auth/token",
        json={"grant_type": "password", "email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_api_key_grant_still_works_and_is_not_throttled(client: TestClient) -> None:
    """A bad service credential is a different axis — no login backoff applies."""
    for _ in range(throttle.THRESHOLD + 2):
        r = client.post(
            "/api/v1/auth/token",
            json={"grant_type": "api_key", "api_key": "titan_ak_nope"},
        )
        # Always 401, never 429 — the api-key path has no per-account bucket.
        assert r.status_code == 401
