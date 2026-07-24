"""Gateway framework tests (Phase 7): §4.1 conventions + §9.1 stack.

The sample /api/v1/widgets router exists only in tests — it exercises the
shared machinery (ETag, pagination, idempotency, problems) exactly the way
Phase-11 CRUD modules will.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import fakeredis.aioredis
import pytest
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.testclient import TestClient

from titan import clients
from titan.gateway.concurrency import require_if_match, set_etag
from titan.gateway.pagination import Page, Pagination, encode_cursor
from titan.gateway.problems import ProblemException, problem_type
from titan.main import create_app
from titan.orchestrator.errors import DependencyCycleError
from titan.settings import Settings

WIDGETS: dict[str, dict[str, Any]] = {}


def _sample_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.post("/widgets", status_code=201)
    async def create_widget(body: dict[str, Any], response: Response) -> dict[str, Any]:
        widget = {"id": str(uuid.uuid4()), "version": 1, **body}
        WIDGETS[widget["id"]] = widget
        set_etag(response, 1)
        return widget

    @router.get("/widgets")
    async def list_widgets(page: Pagination) -> Page[dict[str, Any]]:
        items = sorted(WIDGETS.values(), key=lambda w: w["id"])
        start = 0
        if page.position:
            ids = [w["id"] for w in items]
            start = ids.index(page.position["last"]) + 1 if page.position["last"] in ids else 0
        window = items[start : start + page.limit]
        next_cursor = (
            encode_cursor({"last": window[-1]["id"]})
            if len(items) > start + page.limit and window
            else None
        )
        return Page(items=window, next_cursor=next_cursor)

    @router.get("/widgets/{widget_id}")
    async def get_widget(widget_id: str, response: Response) -> dict[str, Any]:
        widget = WIDGETS.get(widget_id)
        if widget is None:
            raise ProblemException(
                status=404,
                title="Widget not found",
                problem_type=problem_type("not-found"),
            )
        set_etag(response, widget["version"])
        return widget

    @router.patch("/widgets/{widget_id}")
    async def update_widget(
        widget_id: str, body: dict[str, Any], request: Request, response: Response
    ) -> dict[str, Any]:
        widget = WIDGETS[widget_id]
        require_if_match(request, widget["version"])
        widget.update(body)
        widget["version"] += 1
        set_etag(response, widget["version"])
        return widget

    @router.get("/widgets-cycle-demo")
    async def cycle_demo() -> None:
        raise DependencyCycleError(uuid.uuid4(), uuid.uuid4())

    @router.get("/widgets-boom")
    async def boom() -> None:
        raise RuntimeError("kaput")

    @router.post("/widgets-boom", status_code=201)
    async def boom_post(body: dict[str, Any]) -> None:
        raise RuntimeError("kaput")

    @router.post("/widgets-big", status_code=201)
    async def big(body: dict[str, Any]) -> dict[str, Any]:
        # >1 KiB so GZip (innermost) compresses it — the idempotency cache
        # must handle binary bodies (audit D1).
        return {"id": str(uuid.uuid4()), "blob": "x" * 4096}

    return router


def _build_app(monkeypatch: pytest.MonkeyPatch, **settings_overrides: Any) -> FastAPI:
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(clients, "create_redis", lambda _settings: fake)
    # Phase 68: production/staging refuse to boot without crypto keys, so a
    # non-local test app must supply them (overridable via settings_overrides).
    settings = Settings(
        otel_exporter_otlp_endpoint="",
        bus_hmac_key="test-bus-key",
        field_encryption_key="test-field-key",
        _env_file=None,
        **settings_overrides,
    )
    app = create_app(settings)
    app.include_router(_sample_router())
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    WIDGETS.clear()
    with TestClient(_build_app(monkeypatch), raise_server_exceptions=False) as test_client:
        yield test_client


def test_middleware_stack_order_is_spec_exact() -> None:
    app = create_app(Settings(otel_exporter_otlp_endpoint="", _env_file=None))
    names = [getattr(m.cls, "__name__", str(m.cls)) for m in app.user_middleware]
    assert names == [
        "REDMetricsMiddleware",
        "SecurityHeadersMiddleware",
        "RequestContextMiddleware",
        "CORSMiddleware",
        "AuthenticationSlot",
        "RBACSlot",
        "RateLimitMiddleware",
        "IdempotencyMiddleware",
        "DocsAuthGuardMiddleware",
        "BodySizeCapMiddleware",
        "GZipMiddleware",
    ]


def test_problem_shape_on_404(client: TestClient) -> None:
    response = client.get(f"/api/v1/widgets/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert {"type", "title", "status", "instance"} <= body.keys()
    assert body["type"] == "https://titan.dev/errors/not-found"


def test_problem_shape_on_validation_error(client: TestClient) -> None:
    response = client.get("/api/v1/widgets?limit=0")
    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "https://titan.dev/errors/validation"
    assert body["errors"] and "loc" in body["errors"][0]


def test_problem_shape_on_unhandled_exception(client: TestClient) -> None:
    response = client.get("/api/v1/widgets-boom")
    assert response.status_code == 500
    body = response.json()
    assert body["type"] == "https://titan.dev/errors/internal"
    assert "kaput" not in response.text  # no internals leaked


def test_dependency_cycle_maps_to_409_problem(client: TestClient) -> None:
    response = client.get("/api/v1/widgets-cycle-demo")
    assert response.status_code == 409
    assert response.json()["type"] == "https://titan.dev/errors/invalid-dependency-cycle"


def test_if_match_missing_is_428_and_stale_is_412(client: TestClient) -> None:
    created = client.post("/api/v1/widgets", json={"name": "w"})
    assert created.status_code == 201
    assert created.headers["ETag"] == '"v1"'
    wid = created.json()["id"]

    no_header = client.patch(f"/api/v1/widgets/{wid}", json={"name": "x"})
    assert no_header.status_code == 428

    ok = client.patch(f"/api/v1/widgets/{wid}", json={"name": "x"}, headers={"If-Match": '"v1"'})
    assert ok.status_code == 200
    assert ok.headers["ETag"] == '"v2"'

    stale = client.patch(f"/api/v1/widgets/{wid}", json={"name": "y"}, headers={"If-Match": '"v1"'})
    assert stale.status_code == 412
    assert stale.json()["type"] == "https://titan.dev/errors/version-conflict"


def test_idempotency_key_replays_cached_response(client: TestClient) -> None:
    key = {"Idempotency-Key": "same-key-123"}
    first = client.post("/api/v1/widgets", json={"name": "once"}, headers=key)
    second = client.post("/api/v1/widgets", json={"name": "once"}, headers=key)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()  # same widget, handler ran once
    assert second.headers.get("Idempotency-Replayed") == "true"
    assert len(WIDGETS) == 1

    fresh = client.post(
        "/api/v1/widgets", json={"name": "once"}, headers={"Idempotency-Key": "other-key"}
    )
    assert fresh.json()["id"] != first.json()["id"]


def test_idempotency_survives_gzip_sized_bodies(client: TestClient) -> None:
    key = {"Idempotency-Key": "gzip-key", "Accept-Encoding": "gzip"}
    first = client.post("/api/v1/widgets-big", json={}, headers=key)
    second = client.post("/api/v1/widgets-big", json={}, headers=key)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers.get("Idempotency-Replayed") == "true"


def test_idempotency_replay_preserves_etag(client: TestClient) -> None:
    key = {"Idempotency-Key": "etag-key"}
    first = client.post("/api/v1/widgets", json={"name": "e"}, headers=key)
    second = client.post("/api/v1/widgets", json={"name": "e"}, headers=key)
    assert second.headers.get("ETag") == first.headers.get("ETag") == '"v1"'


def test_idempotency_key_not_poisoned_by_handler_exception(client: TestClient) -> None:
    key = {"Idempotency-Key": "boom-key"}
    first = client.post("/api/v1/widgets-boom", json={}, headers=key)
    assert first.status_code == 500
    # Retry must re-run the handler (500 again), never 409 in-flight (audit D4).
    retry = client.post("/api/v1/widgets-boom", json={}, headers=key)
    assert retry.status_code == 500


def test_if_match_wildcard_and_list_accepted(client: TestClient) -> None:
    wid = client.post("/api/v1/widgets", json={"name": "w"}).json()["id"]
    star = client.patch(f"/api/v1/widgets/{wid}", json={"a": 1}, headers={"If-Match": "*"})
    assert star.status_code == 200  # RFC 9110: * matches any representation
    listed = client.patch(
        f"/api/v1/widgets/{wid}", json={"b": 2}, headers={"If-Match": '"v9", W/"v2"'}
    )
    assert listed.status_code == 200  # list + weak prefix tolerated


def test_unhandled_500_still_carries_security_headers(client: TestClient) -> None:
    response = client.get("/api/v1/widgets-boom", headers={"X-Request-ID": "req-500"})
    assert response.status_code == 500
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["X-Request-ID"] == "req-500"


def test_cors_origins_env_accepts_comma_separated() -> None:
    parsed = Settings(
        cors_origins="http://a.dev, http://b.dev",
        _env_file=None,
    )
    assert parsed.cors_origins == ["http://a.dev", "http://b.dev"]
    json_form = Settings(cors_origins='["http://c.dev"]', _env_file=None)
    assert json_form.cors_origins == ["http://c.dev"]


def test_tampered_non_ascii_cursor_is_400(client: TestClient) -> None:
    response = client.get("/api/v1/widgets?cursor=caf%C3%A9")
    assert response.status_code == 400
    assert response.json()["type"] == "https://titan.dev/errors/invalid-cursor"


def test_redoc_is_disabled(client: TestClient) -> None:
    assert client.get("/redoc").status_code == 404


def test_pagination_envelope_and_cursor(client: TestClient) -> None:
    for i in range(5):
        client.post("/api/v1/widgets", json={"name": f"w{i}"})

    page_one = client.get("/api/v1/widgets?limit=2").json()
    assert set(page_one) == {"items", "next_cursor"}
    assert len(page_one["items"]) == 2 and page_one["next_cursor"]

    page_two = client.get(f"/api/v1/widgets?limit=2&cursor={page_one['next_cursor']}").json()
    ids = {w["id"] for w in page_one["items"]} & {w["id"] for w in page_two["items"]}
    assert ids == set()  # no overlap

    bad = client.get("/api/v1/widgets?cursor=%%%%")
    assert bad.status_code == 400
    assert bad.json()["type"] == "https://titan.dev/errors/invalid-cursor"


def test_body_size_cap_413(client: TestClient) -> None:
    response = client.post(
        "/api/v1/widgets",
        content=b"{}",
        headers={"Content-Length": str(3 * 1024 * 1024), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["type"] == "https://titan.dev/errors/payload-too-large"


def test_security_headers_dev_vs_production(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(_build_app(monkeypatch)) as dev_client:
        dev = dev_client.get("/healthz")
    assert dev.headers["X-Content-Type-Options"] == "nosniff"
    assert dev.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Strict-Transport-Security" not in dev.headers

    with TestClient(_build_app(monkeypatch, environment="production")) as prod_client:
        prod = prod_client.get("/healthz")
    assert "max-age" in prod.headers["Strict-Transport-Security"]


def test_docs_require_auth_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(_build_app(monkeypatch, environment="production")) as prod_client:
        docs = prod_client.get("/api/docs")
        openapi = prod_client.get("/api/openapi.json")
    assert docs.status_code == 401
    assert openapi.status_code == 401
    assert docs.json()["type"] == "https://titan.dev/errors/unauthorized"

    with TestClient(_build_app(monkeypatch)) as dev_client:
        assert dev_client.get("/api/docs").status_code == 200
