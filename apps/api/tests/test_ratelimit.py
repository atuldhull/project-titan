"""TITAN-BCK-022 / TITAN-SEC-080: Redis token-bucket rate limiting.

Unit tests drive the ``RateLimiter`` against fakeredis (which runs the REAL
EVALSHA Lua via lupa — not a Python re-implementation). Integration tests drive
the middleware through a TestClient and assert the hard cutoff, the
``X-RateLimit-*`` headers on BOTH 2xx and 429, the ``Retry-After`` on 429, that
excluded probes are never limited, and that internal agent traffic bypasses.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from titan import clients
from titan.gateway.ratelimit import Decision, Limit, RateLimiter, classify
from titan.identity.service import Principal
from titan.main import create_app
from titan.settings import Settings

# ----------------------------------------------------------------------------
# Unit: the limiter itself, against fakeredis + the real Lua script.
# ----------------------------------------------------------------------------


async def _fresh_limiter(**over: Limit) -> RateLimiter:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    classes = {"read": Limit(3, 60, 3), "auth": Limit(2, 60, 2), **over}
    limiter = RateLimiter(redis, classes=classes, layers={}, trusted_proxies=[], org="titan")
    await limiter.load()
    return limiter


def test_bucket_hard_cutoff_and_refill() -> None:
    async def scenario() -> None:
        rl = await _fresh_limiter()
        results = [
            await rl.check(rl.buckets_for(route_class="read", ip="1.2.3.4", principal_key=None))
            for _ in range(5)
        ]
        allowed = [d.allowed for d in results]
        assert allowed == [True, True, True, False, False]  # capacity 3, then denied
        assert results[3].retry_after >= 1  # a positive backoff is reported

    import asyncio

    asyncio.run(scenario())


def test_buckets_are_isolated_per_ip() -> None:
    async def scenario() -> None:
        rl = await _fresh_limiter()
        for _ in range(3):
            await rl.check(rl.buckets_for(route_class="read", ip="1.1.1.1", principal_key=None))
        blocked = await rl.check(
            rl.buckets_for(route_class="read", ip="1.1.1.1", principal_key=None)
        )
        other = await rl.check(rl.buckets_for(route_class="read", ip="2.2.2.2", principal_key=None))
        assert not blocked.allowed
        assert other.allowed  # a different IP has its own bucket

    import asyncio

    asyncio.run(scenario())


def test_auth_class_is_keyed_per_ip_not_principal() -> None:
    rl = RateLimiter(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        classes={"auth": Limit(2, 60, 2)},
        layers={},
        trusted_proxies=[],
        org="titan",
    )
    keys = {k for k, _ in rl.buckets_for(route_class="auth", ip="9.9.9.9", principal_key="user-1")}
    assert "rl:cls:auth:9.9.9.9" in keys  # per-IP, not per-user


def test_fail_open_on_redis_error() -> None:
    class _BrokenScript:
        async def __call__(self, *_a: object, **_k: object) -> object:
            raise RuntimeError("redis down")

    class _BrokenRedis:
        def register_script(self, _script: str) -> object:
            return _BrokenScript()

    async def scenario() -> None:
        rl = RateLimiter(
            _BrokenRedis(),  # type: ignore[arg-type]
            classes={"read": Limit(1, 60, 1)},
            layers={},
            trusted_proxies=[],
            org="titan",
        )
        # Even past the nominal capacity, a Redis outage must ALLOW (fail open).
        for _ in range(5):
            decision = await rl.check(
                rl.buckets_for(route_class="read", ip="1.2.3.4", principal_key=None)
            )
            assert decision.allowed

    import asyncio

    asyncio.run(scenario())


def test_classify_route_classes() -> None:
    assert classify("POST", None, "/api/v1/oauth/token") == "auth"
    assert classify("POST", "project:create", "/api/v1/projects") == "project_create"
    assert classify("POST", "task:transition", "/api/v1/tasks/x:transition") == "transition"
    assert classify("POST", "run:create", "/api/v1/runs") == "transition"
    assert classify("GET", "project:read", "/api/v1/projects/x") == "read"
    assert classify("PATCH", "project:update", "/api/v1/projects/x") == "write"


def test_decision_headers() -> None:
    allowed = Decision(allowed=True, limit=100, remaining=42, retry_after=0, reset=30)
    assert allowed.headers()["X-RateLimit-Limit"] == "100"
    assert allowed.headers()["X-RateLimit-Remaining"] == "42"
    assert "Retry-After" not in allowed.headers()
    denied = Decision(allowed=False, limit=100, remaining=0, retry_after=17, reset=60)
    assert denied.headers()["Retry-After"] == "17"


# ----------------------------------------------------------------------------
# Integration: the middleware through a real app + TestClient.
# ----------------------------------------------------------------------------


def _probe_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/rl-probe")
    async def probe() -> dict[str, str]:
        return {"ok": "yes"}

    return router


def _build_app(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> FastAPI:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(clients, "create_redis", lambda _settings: fake)
    # These ARE the rate-limit tests, so opt back in over the suite-wide default
    # (conftest disables limiting for every other test); overrides still win.
    kwargs = {"rate_limit_enabled": True, **overrides}
    settings = Settings(otel_exporter_otlp_endpoint="", _env_file=None, **kwargs)  # type: ignore[arg-type]
    app = create_app(settings)
    app.include_router(_probe_router())
    return app


@pytest.fixture
def rl_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = _build_app(monkeypatch, rate_limit_read="3/60/3")
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_headers_present_on_2xx(rl_client: TestClient) -> None:
    response = rl_client.get("/api/v1/rl-probe")
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "3"
    assert int(response.headers["X-RateLimit-Remaining"]) == 2
    assert "X-RateLimit-Reset" in response.headers


def test_hard_cutoff_returns_429_with_retry_after(rl_client: TestClient) -> None:
    codes = [rl_client.get("/api/v1/rl-probe").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429 and codes[4] == 429
    blocked = rl_client.get("/api/v1/rl-probe")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.headers["X-RateLimit-Remaining"] == "0"
    assert blocked.headers["content-type"].startswith("application/problem+json")


def test_probe_paths_are_never_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app(monkeypatch, rate_limit_read="1/60/1")
    with TestClient(app, raise_server_exceptions=False) as client:
        for _ in range(10):
            assert client.get("/readyz").status_code in (200, 503)
        # /readyz is excluded → no rate-limit headers, never 429.
        assert "X-RateLimit-Limit" not in client.get("/readyz").headers


def _agent_auth_strategy() -> Any:
    # ONE fixed agent principal across requests — a fresh sub per request would
    # land each in a different per-user bucket and never trip the cap.
    sub, agent_id = uuid.uuid4(), uuid.uuid4()

    async def _agent_auth(request):  # type: ignore[no-untyped-def]
        request.state.principal = Principal(
            sub=sub, kind="agent", roles=[], scope="agent", jti="j", agent_id=agent_id
        )
        return None

    return _agent_auth


def test_internal_agent_skips_the_route_class_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Read class capacity 1, but an agent bypasses the per-route-class bucket.
    app = _build_app(monkeypatch, rate_limit_read="1/60/1")
    app.state.auth_strategy = _agent_auth_strategy()
    with TestClient(app, raise_server_exceptions=False) as client:
        codes = [client.get("/api/v1/rl-probe").status_code for _ in range(5)]
        assert codes == [200, 200, 200, 200, 200]


def test_internal_agent_is_still_capped_by_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    # kind=='agent' is attacker-selectable (any user mints an API key), so an
    # agent must NOT be unlimited — the coarse per-user cap still binds it.
    app = _build_app(monkeypatch, rate_limit_per_user="2/60/2")
    app.state.auth_strategy = _agent_auth_strategy()
    with TestClient(app, raise_server_exceptions=False) as client:
        codes = [client.get("/api/v1/rl-probe").status_code for _ in range(4)]
        assert codes[:2] == [200, 200]
        assert 429 in codes[2:]  # the per-user bucket (capacity 2) stops the flood


def test_disabled_limiter_is_a_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app(monkeypatch, rate_limit_enabled=False, rate_limit_read="1/60/1")
    with TestClient(app, raise_server_exceptions=False) as client:
        codes = [client.get("/api/v1/rl-probe").status_code for _ in range(5)]
        assert codes == [200, 200, 200, 200, 200]
        assert "X-RateLimit-Limit" not in client.get("/api/v1/rl-probe").headers
