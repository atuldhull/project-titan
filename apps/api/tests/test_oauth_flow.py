"""End-to-end OAuth2 Authorization Code + PKCE + MFA (Phase 67 DoD).

Drives the real endpoints against Postgres + fakeredis. Covers TITAN-SEC-010
(PKCE round-trip; implicit and password grants rejected) and TITAN-SEC-015 (org
MFA policy forces a TOTP code at the next privileged login).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import fakeredis.aioredis
import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import run_async
from tests.oauth_helpers import REDIRECT_URI, authorize, login, make_pkce
from titan import clients
from titan.db import uuid7
from titan.identity.service import set_local_password
from titan.main import create_app
from titan.settings import Settings

pytestmark = pytest.mark.schema

EMAIL = "oauth-admin@titan.local"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(
    migrated_engine: Any, schema_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(clients, "create_redis", lambda _settings: fake)

    async def reset() -> None:
        # `migrated_engine` is session-scoped, so auth state (the org policy,
        # MFA credentials, users, audit rows) leaks between tests unless cleared
        # here. A prior test turning `mfa_required` on would otherwise silently
        # change what a later "no MFA" login should do — the session-scoped-DB
        # trap this suite is careful about.
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE org_security_policy, mfa_recovery_codes, "
                    "mfa_credentials, security_events, refresh_tokens, "
                    "user_roles, local_credentials, users RESTART IDENTITY CASCADE"
                )
            )

    run_async(reset)

    async def seed() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO roles (id, name, permissions) "
                    "VALUES (:id, 'owner', '[\"*\"]'::jsonb) ON CONFLICT (name) DO NOTHING"
                ),
                {"id": uuid7()},
            )
            uid = uuid7()
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :email, 'OAuth Admin', 'local', :subj) "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"id": uid, "email": EMAIL, "subj": str(uid)},
            )
            uid = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :email"), {"email": EMAIL}
                )
            ).scalar_one()
            await set_local_password(conn, uid, PASSWORD)
            await conn.execute(
                text(
                    "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                    "SELECT :rid, :uid, r.id, NULL FROM roles r WHERE r.name = 'owner' "
                    "ON CONFLICT (user_id, role_id, project_id) DO NOTHING"
                ),
                {"rid": uuid7(), "uid": uid},
            )

    run_async(seed)
    settings = Settings(database_url=schema_url, otel_exporter_otlp_endpoint="", _env_file=None)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


# --- SEC-010: PKCE round trip and grant rejections --------------------------


def test_full_pkce_round_trip(client: TestClient) -> None:
    pair = login(client, EMAIL, PASSWORD)
    assert pair["access_token"]
    assert pair["refresh_token"].startswith("titan_rt_")
    me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {pair['access_token']}"},
    )
    assert me.status_code == 200 and "owner" in me.json()["roles"]


def test_wrong_verifier_is_rejected(client: TestClient) -> None:
    _verifier, challenge = make_pkce()
    authz = authorize(client, EMAIL, PASSWORD, challenge=challenge)
    assert authz.status_code == 200
    bad = client.post(
        "/api/v1/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": authz.json()["code"],
            "code_verifier": "a" * 50,  # valid length, wrong value
            "redirect_uri": REDIRECT_URI,
        },
    )
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_grant"


def test_a_code_is_single_use(client: TestClient) -> None:
    verifier, challenge = make_pkce()
    authz = authorize(client, EMAIL, PASSWORD, challenge=challenge)
    code = authz.json()["code"]
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
    }
    assert client.post("/api/v1/oauth/token", json=body).status_code == 200
    replay = client.post("/api/v1/oauth/token", json=body)
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"


def test_implicit_flow_is_rejected(client: TestClient) -> None:
    _verifier, challenge = make_pkce()
    r = client.post(
        "/api/v1/oauth/authorize",
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "response_type": "token",  # implicit
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_response_type"


def test_password_grant_is_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/v1/oauth/token",
        json={"grant_type": "password", "username": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_discovery_and_jwks(client: TestClient) -> None:
    disco = client.get("/.well-known/openid-configuration")
    assert disco.status_code == 200
    doc = disco.json()
    assert "password" not in doc["grant_types_supported"]
    assert doc["response_types_supported"] == ["code"]
    assert "S256" in doc["code_challenge_methods_supported"]

    jwks = client.get(doc["jwks_uri"]).json()
    assert jwks["keys"][0]["kty"] == "RSA"
    assert jwks["keys"][0]["kid"]


# --- SEC-015: org MFA policy forces a second factor -------------------------


def _enroll_mfa(client: TestClient, access_token: str) -> str:
    """Enroll + confirm TOTP for the caller; returns the secret."""
    headers = {"Authorization": f"Bearer {access_token}"}
    begin = client.post("/api/v1/auth/mfa/enroll", headers=headers)
    assert begin.status_code == 200, begin.text
    secret = begin.json()["secret"]
    assert begin.json()["qr_png_data_url"].startswith("data:image/png;base64,")
    code = pyotp.TOTP(secret).now()
    confirm = client.post(
        "/api/v1/auth/mfa/confirm", json={"code": code}, headers=headers
    )
    assert confirm.status_code == 200, confirm.text
    assert len(confirm.json()["recovery_codes"]) == 10
    return secret


def test_mfa_policy_forces_totp_at_next_privileged_login(client: TestClient) -> None:
    # 1) Log in without MFA, enroll it, and turn the org policy on.
    pair = login(client, EMAIL, PASSWORD)
    secret = _enroll_mfa(client, pair["access_token"])
    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    policy = client.put(
        "/api/v1/auth/mfa/policy", json={"mfa_required": True}, headers=headers
    )
    assert policy.status_code == 200

    # 2) A fresh login with NO code is now refused (MFA required).
    no_code = authorize(client, EMAIL, PASSWORD)
    assert no_code.status_code == 401
    assert "mfa-required" in no_code.json()["type"]

    # 3) The same login WITH a valid TOTP code succeeds all the way to tokens.
    good = login(client, EMAIL, PASSWORD, totp_code=pyotp.TOTP(secret).now())
    assert good["access_token"]


def test_forced_enrollment_when_privileged_and_unenrolled(client: TestClient) -> None:
    # Turn the policy on while the admin has NO MFA enrolled.
    pair = login(client, EMAIL, PASSWORD)
    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    client.put("/api/v1/auth/mfa/policy", json={"mfa_required": True}, headers=headers)

    # Next login: password is right, but enrollment is required first.
    blocked = authorize(client, EMAIL, PASSWORD)
    assert blocked.status_code == 401
    assert "mfa-enrollment-required" in blocked.json()["type"]


def test_totp_code_cannot_be_replayed(client: TestClient) -> None:
    pair = login(client, EMAIL, PASSWORD)
    secret = _enroll_mfa(client, pair["access_token"])
    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    client.put("/api/v1/auth/mfa/policy", json={"mfa_required": True}, headers=headers)

    code = pyotp.TOTP(secret).now()
    first = login(client, EMAIL, PASSWORD, totp_code=code)
    assert first["access_token"]
    # The same code, immediately reused, is refused (replay guard).
    replay = authorize(client, EMAIL, PASSWORD, totp_code=code)
    assert replay.status_code == 401


def test_recovery_code_works_once(client: TestClient) -> None:
    pair = login(client, EMAIL, PASSWORD)
    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    begin = client.post("/api/v1/auth/mfa/enroll", headers=headers)
    secret = begin.json()["secret"]
    confirm = client.post(
        "/api/v1/auth/mfa/confirm",
        json={"code": pyotp.TOTP(secret).now()},
        headers=headers,
    )
    recovery = confirm.json()["recovery_codes"][0]
    client.put("/api/v1/auth/mfa/policy", json={"mfa_required": True}, headers=headers)

    ok = login(client, EMAIL, PASSWORD, totp_code=recovery)
    assert ok["access_token"]
    # Reusing the burned recovery code fails.
    again = authorize(client, EMAIL, PASSWORD, totp_code=recovery)
    assert again.status_code == 401


def test_refresh_token_grant_is_reachable_at_the_token_endpoint(
    client: TestClient,
) -> None:
    """Regression: validate_grant_type once rejected refresh_token, making the
    token endpoint's own refresh branch dead code while discovery advertised
    it."""
    pair = login(client, EMAIL, PASSWORD)
    rotated = client.post(
        "/api/v1/oauth/token",
        json={"grant_type": "refresh_token", "refresh_token": pair["refresh_token"]},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["access_token"]


def test_agent_api_key_cannot_manage_its_owners_mfa(
    client: TestClient, migrated_engine: Any
) -> None:
    """An API-key principal's sub is the human owner; it must NOT be able to
    enroll a second factor it controls on that account (account-takeover)."""
    pair = login(client, EMAIL, PASSWORD)
    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    created = client.post(
        "/api/v1/auth/api-keys", json={"name": "ci-runner"}, headers=headers
    )
    assert created.status_code == 201, created.text
    api_key = created.json()["api_key"]

    # The agent credential is rejected at the MFA self-management endpoints.
    enroll = client.post("/api/v1/auth/mfa/enroll", headers={"X-API-Key": api_key})
    assert enroll.status_code == 403
    confirm = client.post(
        "/api/v1/auth/mfa/confirm",
        json={"code": "000000"},
        headers={"X-API-Key": api_key},
    )
    assert confirm.status_code == 403


def test_forced_mfa_also_gates_refresh(client: TestClient) -> None:
    """A privileged unenrolled account cannot refresh past the enrollment gate
    (TITAN-SEC-015) — the bypass an interactive-only check would leave open."""
    pair = login(client, EMAIL, PASSWORD)
    refresh_token = pair["refresh_token"]
    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    client.put("/api/v1/auth/mfa/policy", json={"mfa_required": True}, headers=headers)

    # The pre-policy refresh token must not mint fresh tokens for this now-forced
    # account — both the legacy /auth/refresh and the OAuth token endpoint.
    legacy = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
    )
    assert legacy.status_code == 401
    assert "mfa-enrollment-required" in legacy.json()["type"]


def test_refresh_still_works_for_a_non_privileged_user_under_policy(
    client: TestClient, migrated_engine: Any
) -> None:
    """The refresh gate must fire ONLY for privileged unenrolled accounts, not
    break refresh for everyone once the policy is on."""
    viewer_email = "refresh-viewer@titan.local"

    async def seed_viewer() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO roles (id, name, permissions) "
                    "VALUES (:id, 'viewer', '[\"project:read\"]'::jsonb) "
                    "ON CONFLICT (name) DO NOTHING"
                ),
                {"id": uuid7()},
            )
            uid = uuid7()
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :email, 'RViewer', 'local', :subj) "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"id": uid, "email": viewer_email, "subj": str(uid)},
            )
            uid = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :e"), {"e": viewer_email}
                )
            ).scalar_one()
            await set_local_password(conn, uid, PASSWORD)
            await conn.execute(
                text(
                    "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                    "SELECT :rid, :uid, r.id, NULL FROM roles r WHERE r.name = 'viewer' "
                    "ON CONFLICT (user_id, role_id, project_id) DO NOTHING"
                ),
                {"rid": uuid7(), "uid": uid},
            )

    run_async(seed_viewer)
    owner = login(client, EMAIL, PASSWORD)
    client.put(
        "/api/v1/auth/mfa/policy",
        json={"mfa_required": True},
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )
    viewer = login(client, viewer_email, PASSWORD)
    rotated = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": viewer["refresh_token"]}
    )
    assert rotated.status_code == 200, rotated.text


def test_non_privileged_user_is_unaffected_by_policy(
    client: TestClient, migrated_engine: Any
) -> None:
    """A viewer logs in without MFA even when the org policy is on."""
    viewer_email = "viewer@titan.local"

    async def seed_viewer() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO roles (id, name, permissions) "
                    "VALUES (:id, 'viewer', '[\"project:read\"]'::jsonb) "
                    "ON CONFLICT (name) DO NOTHING"
                ),
                {"id": uuid7()},
            )
            uid = uuid7()
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :email, 'Viewer', 'local', :subj) "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"id": uid, "email": viewer_email, "subj": str(uid)},
            )
            uid = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :e"), {"e": viewer_email}
                )
            ).scalar_one()
            await set_local_password(conn, uid, PASSWORD)
            await conn.execute(
                text(
                    "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                    "SELECT :rid, :uid, r.id, NULL FROM roles r WHERE r.name = 'viewer' "
                    "ON CONFLICT (user_id, role_id, project_id) DO NOTHING"
                ),
                {"rid": uuid7(), "uid": uid},
            )

    run_async(seed_viewer)

    # Owner turns the policy on.
    owner = login(client, EMAIL, PASSWORD)
    client.put(
        "/api/v1/auth/mfa/policy",
        json={"mfa_required": True},
        headers={"Authorization": f"Bearer {owner['access_token']}"},
    )
    # Viewer still logs in with no second factor.
    pair = login(client, viewer_email, PASSWORD)
    assert pair["access_token"]
