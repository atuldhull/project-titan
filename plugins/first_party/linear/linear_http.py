"""Stdlib-only HTTP for the Linear connector (Vol 7 §6.4). STDLIB ONLY: a first-party connector
runs in its own isolated venv. The transport is an injectable seam so tests drive the connector
against recorded fixtures with zero network (TITAN-INT-040).
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: str

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body)
        except json.JSONDecodeError:
            return {}


class HttpTransport(Protocol):
    def request(
        self, method: str, url: str, *, headers: dict[str, str], body: str | None = None
    ) -> HttpResponse: ...


class UrllibTransport:
    def __init__(self, *, timeout_s: float = 30.0) -> None:
        self._timeout = timeout_s

    def request(
        self, method: str, url: str, *, headers: dict[str, str], body: str | None = None
    ) -> HttpResponse:
        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method.upper())  # noqa: S310 — https only, allowlisted
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                return HttpResponse(
                    status=resp.status,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=resp.read().decode("utf-8", "replace"),
                )
        except urllib.error.HTTPError as exc:  # a 4xx/5xx is a normal response, not a crash
            return HttpResponse(
                status=exc.code,
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                body=exc.read().decode("utf-8", "replace") if exc.fp else "",
            )


@dataclass
class RecordedTransport:
    """A test transport: pops the next queued response per (METHOD, url-substring) rule."""

    responses: list[tuple[str, str, HttpResponse]] = field(default_factory=list)
    calls: list[tuple[str, str, dict[str, str], str | None]] = field(default_factory=list)

    def add(self, method: str, url_contains: str, response: HttpResponse) -> "RecordedTransport":
        self.responses.append((method.upper(), url_contains, response))
        return self

    def request(
        self, method: str, url: str, *, headers: dict[str, str], body: str | None = None
    ) -> HttpResponse:
        self.calls.append((method.upper(), url, dict(headers), body))
        for i, (m, needle, resp) in enumerate(self.responses):
            if m == method.upper() and needle in url:
                del self.responses[i]
                return resp
        return HttpResponse(404, {}, json.dumps({"message": "no recorded response"}))
