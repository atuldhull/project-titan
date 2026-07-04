"""Factories for the async backing-service clients (TITAN-BCK-002).

Every I/O path is async: SQLAlchemy 2.0 with asyncpg, redis.asyncio, and httpx
for Qdrant's HTTP API. Construction is lazy — no factory opens a connection;
the first caller that awaits does. Instances live on ``app.state`` and are
closed by the application lifespan.
"""

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from titan.settings import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Async SQLAlchemy engine for PostgreSQL (asyncpg driver)."""
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_redis(settings: Settings) -> Redis:
    """Async Redis client (redis.asyncio)."""
    return Redis.from_url(settings.redis_url, decode_responses=True)


def create_http_client(settings: Settings) -> httpx.AsyncClient:
    """Async HTTP client, base-URLed at Qdrant (Vol 3 §12 readiness check)."""
    return httpx.AsyncClient(base_url=settings.qdrant_url, timeout=5.0)
