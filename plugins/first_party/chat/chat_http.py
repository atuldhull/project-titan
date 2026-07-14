"""Stdlib-only HTTP for the chat connector (Vol 7 §6.3). Same rationale as the GitHub
connector: a trusted first party talks to Slack/Discord directly with full headers, depends
on nothing (fast hermetic enable), and the transport is an injectable seam for tests."""

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
        try:
            return json.loads(self.body) if self.body else {}
        except json.JSONDecodeError:
            return {}


class HttpTransport(Protocol):
    def request(
        self, method: str, url: str, *, headers: dict[str, str], body: str | None = None
    ) -> HttpResponse: ...


class UrllibTransport:
    def __init__(self, *, timeout_s: float = 15.0) -> None:
        self._timeout = timeout_s

    def request(
        self, method: str, url: str, *, headers: dict[str, str], body: str | None = None
    ) -> HttpResponse:
        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method.upper())  # noqa: S310 — https, allowlisted
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                return HttpResponse(
                    resp.status,
                    {k.lower(): v for k, v in resp.headers.items()},
                    resp.read().decode("utf-8", "replace"),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                exc.code,
                {k.lower(): v for k, v in (exc.headers or {}).items()},
                exc.read().decode("utf-8", "replace") if exc.fp else "",
            )


@dataclass
class FakeTransport:
    """A scripted transport for tests: a queue of responses (optionally per-URL-substring)
    and a record of every request. A response may be an Exception to simulate a network
    failure (drives the retry/backoff path)."""

    queue: list[HttpResponse | Exception] = field(default_factory=list)
    calls: list[tuple[str, str, dict[str, str], str | None]] = field(
        default_factory=list
    )

    def push(self, item: HttpResponse | Exception) -> "FakeTransport":
        self.queue.append(item)
        return self

    def request(
        self, method: str, url: str, *, headers: dict[str, str], body: str | None = None
    ) -> HttpResponse:
        self.calls.append((method.upper(), url, dict(headers), body))
        item = self.queue.pop(0) if self.queue else HttpResponse(200, {}, "{}")
        if isinstance(item, Exception):
            raise item
        return item
