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
