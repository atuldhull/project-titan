"""Liveness and readiness endpoints (Vol 3 §12).

``/healthz`` is pure liveness — it touches no dependencies. ``/readyz`` checks
PostgreSQL, Redis, and Qdrant concurrently inside the 500 ms budget and
returns 503 naming every failing dependency.
"""

import asyncio
from typing import cast

import httpx
from fastapi import APIRouter, Request, Response
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

router = APIRouter()

_OK = "ok"


async def _check_postgres(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return _OK


async def _check_redis(redis: Redis) -> str:
    await redis.ping()
    return _OK


async def _check_qdrant(http: httpx.AsyncClient) -> str:
    response = await http.get("/readyz")
    response.raise_for_status()
    return _OK


@router.get("/api/v1/healthz")
@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/api/v1/readyz")
@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, object]:
    app_state = request.app.state
    budget = cast(float, app_state.readyz_budget_seconds)

    checks = {
        "postgres": _check_postgres(cast(AsyncEngine, app_state.engine)),
        "redis": _check_redis(cast(Redis, app_state.redis)),
        "qdrant": _check_qdrant(cast(httpx.AsyncClient, app_state.http)),
    }

    async def _run(name: str, check: "asyncio.Future[str] | object") -> tuple[str, str]:
        try:
            async with asyncio.timeout(budget):
                result = await cast("asyncio.Future[str]", check)
            return name, result
        except TimeoutError:
            return name, "timeout"
        except Exception as exc:  # noqa: BLE001 — any failure means not ready
            return name, f"error: {type(exc).__name__}"

    results = dict(await asyncio.gather(*(_run(name, check) for name, check in checks.items())))
    failing = sorted(name for name, status in results.items() if status != _OK)

    if failing:
        response.status_code = 503
        return {"status": "unready", "failing": failing, "checks": results}
    return {"status": "ready", "checks": results}
