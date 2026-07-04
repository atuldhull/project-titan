"""Log-schema tests (TITAN-BCK-024): every line is JSON with the mandatory fields."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from titan.main import create_app

MANDATORY_FIELDS = {"ts", "level", "event", "request_id", "trace_id", "span_id", "module"}


def test_request_logs_are_json_with_mandatory_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with TestClient(create_app()) as client:
        client.get("/healthz", headers={"X-Request-ID": "req-log-test"})

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "expected at least one log line on stdout"

    parsed = [json.loads(line) for line in lines]  # raises if any line is not JSON
    access_lines = [entry for entry in parsed if entry["event"] == "http.request"]
    assert access_lines, "expected an http.request access log line"

    entry = access_lines[-1]
    assert entry.keys() >= MANDATORY_FIELDS
    assert entry["request_id"] == "req-log-test"
    assert entry["path"] == "/healthz"
    assert entry["status"] == 200


def test_stdlib_logging_is_rendered_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    create_app()  # configures logging
    logging.getLogger("uvicorn.error").warning("plain stdlib message")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    entries = [json.loads(line) for line in lines]
    assert any(entry["event"] == "plain stdlib message" for entry in entries)


def test_unhandled_exception_still_logs_the_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaput")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"X-Request-ID": "req-boom"})
    assert response.status_code == 500

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    entries = [json.loads(line) for line in lines]
    access = [e for e in entries if e["event"] == "http.request" and e["path"] == "/boom"]
    assert access, "expected an http.request line for the failed request"
    assert access[-1]["status"] == 500
    assert access[-1]["request_id"] == "req-boom"
