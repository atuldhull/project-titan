"""Phase 79: `titan health` — the dependency-readiness check the worker's K8s exec probe
runs (OPS-008). Proves it exits 0 when Postgres+Redis are reachable and 1 (fail closed)
when a dependency is down, so a bad DSN stalls the rollout instead of running blind."""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from titan import clients
from titan.cli import main

pytestmark = pytest.mark.schema


class _BrokenRedis:
    async def ping(self) -> None:
        raise RuntimeError("connection refused")

    async def aclose(self) -> None:
        return None


def _patch_engine(monkeypatch: pytest.MonkeyPatch, schema_url: str) -> None:
    monkeypatch.setattr(
        clients, "create_engine", lambda _s: create_async_engine(schema_url, poolclass=NullPool)
    )


def test_health_ready_when_deps_up(
    migrated_engine: Any,
    schema_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_engine(monkeypatch, schema_url)
    monkeypatch.setattr(
        clients, "create_redis", lambda _s: fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    assert main(["health"]) == 0
    assert "ready" in capsys.readouterr().out


def test_health_unready_when_redis_down(
    migrated_engine: Any,
    schema_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_engine(monkeypatch, schema_url)
    monkeypatch.setattr(clients, "create_redis", lambda _s: _BrokenRedis())
    assert main(["health"]) == 1
    assert "UNREADY" in capsys.readouterr().err
