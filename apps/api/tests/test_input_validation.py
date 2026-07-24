"""TITAN-SEC-070 / SEC-076: strict input validation on every request body.

Three guarantees, each with its own test:

* every Pydantic model used as a REQUEST body (including inside a union) sets
  ``extra="forbid"`` — enforced by a census over the built app's routes, so a
  future permissive body is caught here rather than in a fuzz run months later;
* the census actually bites — a synthetic permissive body is flagged (the gate
  is not vacuous);
* public endpoints never return a 500 on adversarial JSON (hypothesis fuzz);
* a 422 validation error never echoes the raw input value back (SEC-070's "no
  echo" clause, which the generic error handler owns).

No DB is needed for the census; the fuzz/echo tests drive the ``client`` fixture.
"""

from __future__ import annotations

import inspect
import typing

import pytest
from fastapi import params as fastapi_params
from fastapi.routing import APIRoute
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from titan.main import create_app
from titan.settings import Settings

pytestmark = pytest.mark.schema  # the fuzz/echo tests hit the real schema


@pytest.fixture
def public_client(migrated_engine, schema_url, monkeypatch):  # type: ignore[no-untyped-def]
    """A TestClient over the real schema with rate limiting OFF, so a fuzz run
    exercises validation on every request instead of being masked by 429s."""
    import fakeredis.aioredis
    from fastapi.testclient import TestClient

    from titan import clients

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(clients, "create_redis", lambda _settings: fake)
    settings = Settings(
        database_url=schema_url,
        otel_exporter_otlp_endpoint="",
        rate_limit_enabled=False,
        _env_file=None,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def _models_in(annotation: object) -> list[type[BaseModel]]:
    """Every Pydantic model reachable in a (possibly unioned/optional) type."""
    origin = typing.get_origin(annotation)
    if origin is not None:
        found: list[type[BaseModel]] = []
        for arg in typing.get_args(annotation):
            found.extend(_models_in(arg))
        return found
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return []


# Param markers that mean "NOT a request body" (injected dependency, query,
# path, header, cookie). Security subclasses Depends. Form/Body are omitted on
# purpose: a form/JSON body model IS in scope for SEC-070.
_NON_BODY_MARKERS = (
    fastapi_params.Depends,
    fastapi_params.Query,
    fastapi_params.Path,
    fastapi_params.Header,
    fastapi_params.Cookie,
)


def _is_non_body(param: inspect.Parameter, annotation: object) -> bool:
    """A param is non-body if its default OR its Annotated metadata is a
    dependency/query/path/header/cookie marker (both FastAPI spellings)."""
    if isinstance(param.default, _NON_BODY_MARKERS):
        return True
    if typing.get_origin(annotation) is typing.Annotated:
        return any(isinstance(meta, _NON_BODY_MARKERS) for meta in typing.get_args(annotation)[1:])
    return False


def _request_body_models(endpoint: object) -> list[type[BaseModel]]:
    """The Pydantic models an endpoint accepts as a request body."""
    hints = typing.get_type_hints(endpoint, include_extras=True)  # keep Annotated metadata
    sig = inspect.signature(endpoint)  # type: ignore[arg-type]
    models: list[type[BaseModel]] = []
    for name, param in sig.parameters.items():
        if name == "return":
            continue
        annotation = hints.get(name, param.annotation)
        if _is_non_body(param, annotation):
            continue
        # Unwrap Annotated[T, ...] to T before collecting models.
        if typing.get_origin(annotation) is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
        models.extend(_models_in(annotation))
    return models


# Documented SEC-070 exceptions: request bodies that must NOT forbid extra.
# TokenExchangeRequest is the OAuth /token body — RFC 6749 requires it to accept
# grant-type-specific parameters and answer with an OAuth error, not a 422.
_FORBID_EXEMPT = {"titan.identity.oauth_router.TokenExchangeRequest"}


def _scan(routes: list[APIRoute]) -> tuple[set[type[BaseModel]], dict[str, str]]:
    """Return (all body models seen, {qualname -> where} for the permissive ones)."""
    seen: set[type[BaseModel]] = set()
    offenders: dict[str, str] = {}
    for route in routes:
        for model in _request_body_models(route.endpoint):
            seen.add(model)
            qualname = f"{model.__module__}.{model.__qualname__}"
            if qualname in _FORBID_EXEMPT:
                continue
            if model.model_config.get("extra") != "forbid":
                offenders[f"{model.__module__}.{model.__qualname__}"] = (
                    f"{sorted(route.methods or [])} {route.path}"
                )
    return seen, offenders


class _PermissiveBody(BaseModel):  # module-level so get_type_hints can resolve it
    field: str


async def _permissive_endpoint(body: _PermissiveBody) -> dict[str, str]:  # pragma: no cover
    return {}


def test_every_request_body_model_forbids_extra() -> None:
    """SEC-070: no request body silently accepts unknown keys."""
    from titan.gateway.route_guard import api_routes
    from titan.identity.router import PasswordGrant

    app = create_app(Settings(otel_exporter_otlp_endpoint="", _env_file=None, log_level="CRITICAL"))
    seen, offenders = _scan(api_routes(app))
    # Non-vacuous: the scan actually reaches real request bodies, incl. a model
    # inside a union body (PasswordGrant is `token(grant: PasswordGrant | ...)`).
    assert PasswordGrant in seen
    assert len(seen) > 30
    assert not offenders, "request-body models missing extra='forbid':\n" + "\n".join(
        f"  {model}  <-  {where}" for model, where in sorted(offenders.items())
    )


def test_census_catches_a_permissive_body() -> None:
    """The census is not vacuous: a bare-BaseModel body IS flagged."""
    route = APIRoute("/x", _permissive_endpoint, methods=["POST"])
    _seen, offenders = _scan([route])
    assert any("_PermissiveBody" in name for name in offenders)


# Public POST endpoints (no JWT) — the SEC-070 fuzz surface.
_PUBLIC_POST_PATHS = ("/api/v1/auth/token", "/api/v1/auth/refresh")


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    payload=st.recursive(
        st.none()
        | st.booleans()
        | st.integers()
        # inf/nan are not JSON-serializable — the client rejects them before they
        # reach the server, so they test httpx, not TITAN. Real JSON is finite.
        | st.floats(allow_nan=False, allow_infinity=False)
        | st.text(),
        lambda children: (
            st.lists(children, max_size=4)
            | st.dictionaries(st.text(max_size=8), children, max_size=4)
        ),
        max_leaves=8,
    )
)
def test_public_endpoints_never_500_on_fuzzed_json(public_client, payload) -> None:  # type: ignore[no-untyped-def]
    """SEC-070 acceptance: adversarial JSON on public endpoints never 500s."""
    for path in _PUBLIC_POST_PATHS:
        response = public_client.post(path, json=payload)
        assert response.status_code < 500, (path, payload, response.text)


def test_422_does_not_echo_raw_input(public_client) -> None:  # type: ignore[no-untyped-def]
    """SEC-070/SEC-076: a validation error never reflects the submitted value."""
    secret = "s3cr3t-should-not-appear-in-the-response"
    response = public_client.post(
        "/api/v1/auth/token",
        json={"grant_type": "password", "email": secret, "password": 12345, "junk": secret},
    )
    assert response.status_code == 422
    assert secret not in response.text
