"""Offline identity flow tests (Phase 8 DoD): login → rotate → reuse →
family revocation; ≤60 s revocation; API-key hash-at-rest. Needs the
disposable test DB (schema marker) + fakeredis — zero network."""

import uuid
from collections.abc import Iterator
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.conftest import run_async
from titan import clients
from titan.db import uuid7
from titan.identity.service import set_local_password
from titan.main import create_app
from titan.settings import Settings

pytestmark = pytest.mark.schema

EMAIL = "flow-admin@titan.local"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def client(
    migrated_engine: Any, schema_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(clients, "create_redis", lambda _settings: fake)

    async def seed_user() -> None:
        async with migrated_engine.begin() as conn:
            user_id = uuid7()
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                    "VALUES (:id, :email, 'Flow Admin', 'local', :subj) "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"id": user_id, "email": EMAIL, "subj": str(user_id)},
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

    run_async(ensure_roles)
    run_async(seed_user)

    settings = Settings(database_url=schema_url, otel_exporter_otlp_endpoint="", _env_file=None)
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def _login(client: TestClient) -> dict[str, Any]:
    # User login is the Authorization Code + PKCE flow now (TITAN-SEC-010); the
    # password grant is rejected. `oauth_helpers.login` drives both legs.
    from tests.oauth_helpers import login

    return login(client, EMAIL, PASSWORD)


def test_login_me_round_trip_offline(client: TestClient) -> None:
    pair = _login(client)
    assert pair["token_type"] == "Bearer"
    assert pair["expires_in"] <= 900
    assert pair["refresh_token"].startswith("titan_rt_")

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {pair['access_token']}"})
    assert me.status_code == 200
    body = me.json()
    assert body["kind"] == "user" and "owner" in body["roles"]


def test_wrong_password_is_401_problem(client: TestClient) -> None:
    from tests.oauth_helpers import authorize

    response = authorize(client, EMAIL, "wrong")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_refresh_rotation_and_family_reuse_revocation(client: TestClient) -> None:
    pair = _login(client)
    first_refresh = pair["refresh_token"]

    rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first_refresh

    # Replaying the consumed token must revoke the ENTIRE family…
    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": first_refresh})
    assert replay.status_code == 401

    # …including the newest refresh token…
    dead = client.post("/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert dead.status_code == 401

    # …and outstanding ACCESS tokens from that family (fam claim on the
    # denylist), well within the 60 s bound — immediately, in fact.
    me = client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {second['access_token']}"}
    )
    assert me.status_code == 401


def test_revoke_endpoint_takes_effect_immediately(client: TestClient) -> None:
    pair = _login(client)
    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    assert client.post("/api/v1/auth/revoke", headers=headers).status_code == 204
    # The denylist applies on the next request.
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401, (
        "TITAN-SEC-014: revocation takes effect within ≤60 s (next request)"
    )


def test_logout_durably_kills_the_refresh_family(client: TestClient) -> None:
    """After logout, a captured refresh token must be permanently unusable —
    not merely until a Redis TTL expires (audit #1)."""
    pair = _login(client)
    headers = {"Authorization": f"Bearer {pair['access_token']}"}
    assert client.post("/api/v1/auth/revoke", headers=headers).status_code == 204
    dead = client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert dead.status_code == 401


def test_stale_credential_does_not_block_public_endpoints(client: TestClient) -> None:
    """A garbage/stale bearer must pass through on public paths (audit #2):
    /healthz, /oauth/authorize, /oauth/token and /auth/refresh must still work
    with a junk Authorization header attached."""
    from tests.oauth_helpers import make_pkce

    junk = {"Authorization": "Bearer not-a-real-jwt"}
    assert client.get("/healthz", headers=junk).status_code == 200

    verifier, challenge = make_pkce()
    authz = client.post(
        "/api/v1/oauth/authorize",
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "response_type": "code",
            "redirect_uri": "http://localhost:3000/api/auth/callback",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        headers=junk,
    )
    assert authz.status_code == 200, authz.text

    exchanged = client.post(
        "/api/v1/oauth/token",
        json={
            "grant_type": "authorization_code",
            "code": authz.json()["code"],
            "code_verifier": verifier,
            "redirect_uri": "http://localhost:3000/api/auth/callback",
        },
        headers=junk,
    )
    assert exchanged.status_code == 200

    # The classic footgun: a stale access token attached to the refresh call.
    rotated = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": exchanged.json()["refresh_token"]},
        headers=junk,
    )
    assert rotated.status_code == 200


def test_api_key_end_to_end_with_only_hash_at_rest(
    client: TestClient, migrated_engine: Any
) -> None:
    pair = _login(client)
    agent_id = str(uuid.uuid4())
    created = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "ci-runner", "agent_id": agent_id},
        headers={"Authorization": f"Bearer {pair['access_token']}"},
    )
    assert created.status_code == 201
    raw_key = created.json()["api_key"]
    assert raw_key.startswith("titan_ak_")

    # Only the SHA-256 at rest — the raw key appears nowhere in the DB.
    async def db_check() -> tuple[int, int]:
        async with migrated_engine.connect() as conn:
            raw_hits = await conn.execute(
                text("SELECT count(*) FROM api_keys WHERE key_hash = :raw OR prefix = :raw"),
                {"raw": raw_key},
            )
            rows = await conn.execute(text("SELECT count(*) FROM api_keys"))
            return raw_hits.scalar_one(), rows.scalar_one()

    raw_hits, total = run_async(db_check)
    assert raw_hits == 0 and total >= 1

    # X-API-Key authenticates as an agent principal with the operator bundle.
    me = client.get("/api/v1/auth/me", headers={"X-API-Key": raw_key})
    assert me.status_code == 200
    body = me.json()
    assert body["kind"] == "agent"
    assert body["roles"] == ["operator"]
    assert body["agent_id"] == agent_id

    # grant_type=api_key exchange returns a service JWT with agent_id claim.
    exchanged = client.post(
        "/api/v1/auth/token", json={"grant_type": "api_key", "api_key": raw_key}
    )
    assert exchanged.status_code == 200
    service_me = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {exchanged.json()['access_token']}"},
    )
    assert service_me.status_code == 200 and service_me.json()["kind"] == "agent"

    # Bad key fails closed.
    assert client.get("/api/v1/auth/me", headers={"X-API-Key": "titan_ak_nope"}).status_code == 401


def test_me_without_credentials_is_401(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401


def test_lockdown_revoke_all_sessions_closes_the_refresh_bypass(
    client: TestClient, migrated_engine: Any
) -> None:
    """SEC-120 review #1: the incident-lockdown session sweep durably revokes refresh
    tokens, so a held refresh token cannot re-mint privileges after lockdown."""
    from titan.identity.service import revoke_all_sessions

    pair = _login(client)
    rotated = client.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert rotated.status_code == 200  # works before lockdown
    fresh_refresh = rotated.json()["refresh_token"]

    run_async(lambda: revoke_all_sessions(migrated_engine))  # lockdown sweep

    denied = client.post("/api/v1/auth/refresh", json={"refresh_token": fresh_refresh})
    assert denied.status_code == 401  # the bypass is closed
