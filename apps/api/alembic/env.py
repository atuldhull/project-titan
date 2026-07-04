"""Alembic environment — async engine, URL from titan.settings (TITAN-BCK-002)."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from titan.observability.logging import configure_logging
from titan.settings import get_settings

config = context.config

# Migration output obeys TITAN-BCK-024 like every other process: JSON to stdout.
configure_logging(get_settings().log_level)

# Models arrive in Phase 6; migrations are hand-written (see alembic/README),
# so no autogenerate metadata is wired on purpose.
target_metadata = None


def _database_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Render SQL without a live database (--sql mode)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)  # type: ignore[arg-type]
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync_migrations)
        await connection.commit()
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
