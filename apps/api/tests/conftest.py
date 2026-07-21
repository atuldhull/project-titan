"""Shared fixtures. Schema tests need a live PostgreSQL 16.

Set ``TITAN_TEST_DATABASE_URL`` (e.g.
``postgresql+asyncpg://titan:titan@localhost:25432/titan_test``) to run the
``schema``-marked tests; without it they skip. The fixture recreates the
database schema from migrations on every session, so NEVER point it at a
database whose contents you care about — it is deliberately a separate
variable from TITAN_DATABASE_URL for exactly that reason.
"""

import asyncio
import os
from collections.abc import Callable, Iterator
from functools import lru_cache
from typing import Any, TypeVar

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from titan.settings import get_settings

T = TypeVar("T")

TEST_DB_ENV = "TITAN_TEST_DATABASE_URL"


def run_async(factory: "Callable[[], Any]") -> Any:
    """Run a coroutine from sync test code (no pytest-asyncio dependency)."""
    return asyncio.run(factory())


def _ensure_database(url: str) -> None:
    """Create the target database if missing (connects to the admin DB)."""
    admin_url, _, dbname = url.rpartition("/")

    async def ensure() -> None:
        engine = create_async_engine(
            f"{admin_url}/postgres", isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        try:
            async with engine.connect() as conn:
                exists = await conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}
                )
                if exists.scalar() is None:
                    await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
        finally:
            await engine.dispose()

    run_async(ensure)


def upgrade_head(url: str) -> None:
    """Run alembic upgrade head against ``url`` in-process."""
    os.environ["TITAN_DATABASE_URL"] = url
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")


def downgrade_base(url: str) -> None:
    os.environ["TITAN_DATABASE_URL"] = url
    get_settings.cache_clear()
    command.downgrade(Config("alembic.ini"), "base")


@pytest.fixture(scope="session")
def schema_url() -> str:
    url = os.environ.get(TEST_DB_ENV)
    if not url:
        pytest.skip(f"{TEST_DB_ENV} not set — schema tests need a disposable PostgreSQL 16")
    return url


@pytest.fixture(scope="session")
def migrated_engine(schema_url: str) -> Iterator[AsyncEngine]:
    """Fresh schema from migrations, once per session."""
    _ensure_database(schema_url)

    async def reset_schema() -> None:
        engine = create_async_engine(schema_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DROP SCHEMA public CASCADE"))
                # Non-public schemas (brain, Phase 33) survive a public drop, so
                # clear them too or the from-base re-migration hits existing tables.
                await conn.execute(text("DROP SCHEMA IF EXISTS brain CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
        finally:
            await engine.dispose()

    run_async(reset_schema)
    upgrade_head(schema_url)

    # NullPool: every test runs its own asyncio.run() loop; pooled asyncpg
    # connections must never outlive the loop that created them.
    engine = create_async_engine(schema_url, poolclass=NullPool)
    yield engine
    run_async(engine.dispose)


# Roles seeded by the `api` fixture (the §9.3 bundles from scripts.seed).
_ROLES = ("owner", "admin", "maintainer", "operator", "viewer")


@pytest.fixture(autouse=True)
def _reset_auth_policy(request: pytest.FixtureRequest) -> "Iterator[None]":
    """Clear the Phase-67 GLOBAL auth policy before every schema test.

    `org_security_policy` is a persistent org-wide setting, so a test that turns
    `mfa_required` on would force-enroll every owner/admin login in a LATER test
    on the session-scoped engine — a Phase-8 login test with no MFA awareness
    would suddenly fail because a neighbouring OAuth test left the policy on.
    This ran green alone and red in-suite until this reset existed (the
    session-scoped-DB trap, again). The MFA credential tables are cleared too so
    a reused test email can never inherit a stale enrollment.

    Autouse and function-scoped, but a no-op for non-schema tests (which have no
    database), so it never forces the engine up for a pure-unit test.
    """
    if request.node.get_closest_marker("schema") is None:
        yield
        return
    engine = request.getfixturevalue("migrated_engine")

    async def clear() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    # Phase 68: secret_metadata (unique names) and secret_audit
                    # (lifecycle counts) are session-persistent global state too —
                    # a fixed secret name or an audit-count assertion would carry
                    # across tests without this (the same session-scoped-DB trap).
                    "TRUNCATE org_security_policy, mfa_recovery_codes, "
                    "mfa_credentials, secret_metadata, secret_audit "
                    "RESTART IDENTITY CASCADE"
                )
            )

    run_async(clear)
    yield


@pytest.fixture(autouse=True)
def _reset_redaction_engine() -> "Iterator[None]":
    """Restore the PROCESS-GLOBAL redaction engine after every test.

    Phase 69: every default-wired sink (gateway, log processor, tool bus,
    output streams) reads `titan.redaction.runtime.default_engine()`, and the
    composition roots mutate it in place (inventory digests, hit reporter). A
    test that loads an inventory would otherwise redact a same-named value in
    an unrelated LATER test — the session-global-state trap, in-process this
    time. Cheap enough to run for every test, unit tests included.
    """
    from titan.redaction import reset_default_engine

    reset_default_engine()
    yield
    reset_default_engine()


@lru_cache(maxsize=1)
def _test_jwt_key_b64() -> str:
    """A stable RSA keypair for the test session so a token minted by one app
    instance still verifies after a 'restart' (a fresh create_app). Without a
    fixed key each app mints an ephemeral keypair and cross-instance tokens 401.
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(pem).decode()


@pytest.fixture
def api(
    migrated_engine: AsyncEngine, schema_url: str, monkeypatch: pytest.MonkeyPatch
) -> "Iterator[dict[str, Any]]":
    """A TestClient over the real schema with the 5 §9.3 roles + one user each
    (org-wide) + a project-scoped-only user seeded, fakeredis, and helpers:
    ``token_for(role)``, ``user_ids``, ``grant_project_role`` and
    ``new_client()`` (a fresh app instance on the SAME DB, for restart tests).
    """
    import uuid

    import fakeredis.aioredis
    from fastapi.testclient import TestClient
    from scripts.seed import ROLE_PERMISSIONS

    from titan import clients
    from titan.db import uuid7
    from titan.main import create_app
    from titan.settings import Settings

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(clients, "create_redis", lambda _settings: fake)

    user_ids: dict[str, uuid.UUID] = {}

    async def seed() -> None:
        import json as _json

        async with migrated_engine.begin() as conn:
            for name, perms in ROLE_PERMISSIONS.items():
                await conn.execute(
                    text(
                        "INSERT INTO roles (id, name, permissions) VALUES (:id, :n, :p) "
                        "ON CONFLICT (name) DO UPDATE SET permissions = :p"
                    ),
                    {"id": uuid7(), "n": name, "p": _json.dumps(perms)},
                )
            for role in (*_ROLES, "scoped"):
                uid = uuid7()
                user_ids[role] = uid
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, display_name, auth_provider, auth_subject)"
                        " VALUES (:id, :email, :dn, 'local', :subj)"
                    ),
                    {"id": uid, "email": f"{role}-{uid}@titan.local", "dn": role, "subj": str(uid)},
                )
                if role in _ROLES:
                    await conn.execute(
                        text(
                            "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                            "SELECT :rid, :uid, r.id, NULL FROM roles r WHERE r.name = :role"
                        ),
                        {"rid": uuid7(), "uid": uid, "role": role},
                    )

    run_async(seed)

    settings = Settings(
        database_url=schema_url,
        jwt_private_key_b64=_test_jwt_key_b64(),
        otel_exporter_otlp_endpoint="",
        _env_file=None,
    )
    app = create_app(settings)

    def grant_project_role(role_user: str, project_id: str, role: str) -> None:
        async def grant() -> None:
            async with migrated_engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO user_roles (id, user_id, role_id, project_id) "
                        "SELECT :rid, :uid, r.id, :pid FROM roles r WHERE r.name = :role"
                    ),
                    {"rid": uuid7(), "uid": user_ids[role_user], "pid": project_id, "role": role},
                )

        run_async(grant)

    with TestClient(app, raise_server_exceptions=False) as client:

        def token_for(role: str, roles: "list[str] | None" = None) -> dict[str, str]:
            access, _ = app.state.keyring.mint(
                sub=str(user_ids[role]),
                roles=roles if roles is not None else ([role] if role in _ROLES else []),
                scope="user",
            )
            return {"Authorization": f"Bearer {access}"}

        def new_client() -> TestClient:
            """A fresh app on the SAME database — a process 'restart'."""
            return TestClient(create_app(settings), raise_server_exceptions=False)

        yield {
            "client": client,
            "token_for": token_for,
            "grant_project_role": grant_project_role,
            "user_ids": user_ids,
            "engine": migrated_engine,
            "new_client": new_client,
        }
