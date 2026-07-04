"""Health endpoint tests (Vol 3 §12): liveness, readiness, dependency-down."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from titan.main import create_app
from titan.observability import health


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_healthz_is_alive_without_dependencies(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_healthz_api_v1_alias(client: TestClient) -> None:
    assert client.get("/api/v1/healthz").status_code == 200


def _stub_check(result: str = "ok", *, fail: bool = False) -> Any:
    async def check(_: Any) -> str:
        if fail:
            raise ConnectionError("dependency down")
        return result

    return check


def test_readyz_200_when_all_dependencies_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "_check_postgres", _stub_check())
    monkeypatch.setattr(health, "_check_redis", _stub_check())
    monkeypatch.setattr(health, "_check_qdrant", _stub_check())

    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"postgres": "ok", "redis": "ok", "qdrant": "ok"}


def test_readyz_503_names_the_failing_dependency(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "_check_postgres", _stub_check())
    monkeypatch.setattr(health, "_check_redis", _stub_check(fail=True))
    monkeypatch.setattr(health, "_check_qdrant", _stub_check())

    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unready"
    assert body["failing"] == ["redis"]
    assert body["checks"]["redis"].startswith("error:")


def test_readyz_times_out_within_budget(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def hang(_: Any) -> str:
        await asyncio.sleep(5)
        return "ok"

    monkeypatch.setattr(health, "_check_postgres", hang)
    monkeypatch.setattr(health, "_check_redis", _stub_check())
    monkeypatch.setattr(health, "_check_qdrant", _stub_check())

    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["postgres"] == "timeout"


def test_response_carries_request_id_header(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.headers.get("X-Request-ID")


def test_inbound_request_id_is_honored(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "req-fixed-123"})
    assert response.headers["X-Request-ID"] == "req-fixed-123"
