"""Liveness and readiness endpoints (Vol 3 §12).

``/healthz`` is pure liveness — it touches no dependencies. ``/readyz`` checks
PostgreSQL, Redis, and Qdrant concurrently inside the 500 ms budget and
returns 503 naming every failing dependency. Response shapes are Pydantic
models so they land in the OpenAPI document and the generated TypeScript
client (TITAN-BCK-027).
"""

import asyncio
from typing import Literal, cast

import httpx
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.observability.metrics import READYZ_DEPENDENCY_UP

router = APIRouter()

_OK = "ok"


class HealthzResponse(BaseModel):
    """Liveness probe response."""

    status: Literal["alive"]


class ReadyzResponse(BaseModel):
    """Readiness probe response; ``failing`` is non-empty exactly when unready."""

    status: Literal["ready", "unready"]
    checks: dict[str, str]
    failing: list[str] = Field(default_factory=list)


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


@router.get("/api/v1/healthz", response_model=HealthzResponse)
@router.get("/healthz", response_model=HealthzResponse)
async def healthz() -> HealthzResponse:
    return HealthzResponse(status="alive")


@router.get(
    "/api/v1/readyz",
    response_model=ReadyzResponse,
    responses={503: {"model": ReadyzResponse, "description": "One or more dependencies failing"}},
)
@router.get(
    "/readyz",
    response_model=ReadyzResponse,
    responses={503: {"model": ReadyzResponse, "description": "One or more dependencies failing"}},
)
async def readyz(request: Request, response: Response) -> ReadyzResponse:
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

    for name, status in results.items():
        READYZ_DEPENDENCY_UP.labels(dependency=name).set(1 if status == _OK else 0)

    if failing:
        response.status_code = 503
        return ReadyzResponse(status="unready", failing=failing, checks=results)
    return ReadyzResponse(status="ready", checks=results)
