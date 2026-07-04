"""Metrics and trace-correlation tests (Vol 3 §12, TITAN-BCK-025)."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider

from titan.main import create_app
from titan.observability import health, otel
from titan.observability.logging import _add_trace_ids
from titan.settings import Settings


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Explicit empty endpoint: the suite must never export traces, even if the
    # developer's shell has TITAN_OTEL_EXPORTER_OTLP_ENDPOINT set.
    settings = Settings(otel_exporter_otlp_endpoint="", _env_file=None)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_tracing_disabled_without_endpoint() -> None:
    settings = Settings(otel_exporter_otlp_endpoint="", _env_file=None)
    assert otel.tracing_enabled(settings) is False
    assert otel.setup_tracing(settings, service_name="test") is False


def test_metrics_endpoint_serves_prometheus_format(client: TestClient) -> None:
    client.get("/healthz")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "titan_http_request_duration_seconds" in response.text


def test_red_histogram_uses_route_template_labels(client: TestClient) -> None:
    client.get("/healthz")
    body = client.get("/metrics").text
    assert 'route="/healthz"' in body
    assert 'method="GET"' in body


def test_unmatched_routes_collapse_to_single_label(client: TestClient) -> None:
    client.get("/no/such/path-12345")
    body = client.get("/metrics").text
    assert 'route="unmatched"' in body
    assert "path-12345" not in body


def test_readyz_updates_dependency_gauge(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok(_: Any) -> str:
        return "ok"

    async def down(_: Any) -> str:
        raise ConnectionError("down")

    monkeypatch.setattr(health, "_check_postgres", ok)
    monkeypatch.setattr(health, "_check_redis", down)
    monkeypatch.setattr(health, "_check_qdrant", ok)
    client.get("/readyz")

    body = client.get("/metrics").text
    assert 'titan_readyz_dependency_up{dependency="postgres"} 1.0' in body
    assert 'titan_readyz_dependency_up{dependency="redis"} 0.0' in body


def test_log_processor_injects_active_span_ids() -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("test-span") as span:
        event = _add_trace_ids(None, "info", {})
        context = span.get_span_context()
        assert event["trace_id"] == format(context.trace_id, "032x")
        assert event["span_id"] == format(context.span_id, "016x")


def test_log_processor_null_ids_without_span() -> None:
    event = _add_trace_ids(None, "info", {})
    assert event["trace_id"] is None
    assert event["span_id"] is None
