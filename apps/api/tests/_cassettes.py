"""Record/replay cassette harness for adapter wire conformance (Phase 16).

A cassette is a committed JSON fixture: an ordered list of interactions, each a
request fingerprint (method + path, query scrubbed) and the provider's response
(``json`` for unary calls, ``sse`` line list for streams). In CI — no keys, no
network — :class:`CassetteReplayTransport` serves them deterministically. When
keys are present, :class:`RecordingTransport` wraps a real transport and writes a
fresh cassette; secrets never enter it because only method+scrubbed-path and the
response body are stored (auth rides in request headers/query, which are dropped).

The same cassettes replay for every adapter, so the golden transcript proves the
four backends normalize to one canonical transcript (TITAN-INT-060/061).
"""

import json
from pathlib import Path
from typing import Any

import httpx

CASSETTE_DIR = Path(__file__).parent / "cassettes"


def _scrub_path(url: httpx.URL) -> str:
    """The path with any credential-bearing query dropped (e.g. Gemini ?key=)."""
    params = url.params.remove("key") if "key" in url.params else url.params
    return url.path + (f"?{params}" if params else "")


def _response_from(interaction: dict[str, Any], request: httpx.Request) -> httpx.Response:
    resp = interaction["response"]
    if "sse" in resp:
        # Each item is a raw string (e.g. "[DONE]") or an object serialized to
        # the SSE data payload — objects keep hand-authored cassettes readable.
        lines = [item if isinstance(item, str) else json.dumps(item) for item in resp["sse"]]
        body = "".join(f"data: {line}\n\n" for line in lines).encode()
        return httpx.Response(resp["status"], content=body, request=request)
    return httpx.Response(resp["status"], json=resp.get("json", {}), request=request)


class CassetteReplayTransport(httpx.AsyncBaseTransport):
    """Serves recorded interactions in order — no network, keyless."""

    def __init__(self, interactions: list[dict[str, Any]]) -> None:
        self._interactions = interactions
        self._index = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._index >= len(self._interactions):
            raise AssertionError(
                f"cassette exhausted: no interaction #{self._index} for "
                f"{request.method} {_scrub_path(request.url)}"
            )
        interaction = self._interactions[self._index]
        self._index += 1
        return _response_from(interaction, request)


class RecordingTransport(httpx.AsyncBaseTransport):
    """Wraps a real transport, forwarding requests and recording scrubbed
    interactions. Call :meth:`dump` to persist the cassette after the transcript."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.interactions: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        body = await response.aread()  # buffer so we can both record and return it
        record: dict[str, Any] = {
            "request": {"method": request.method, "path": _scrub_path(request.url)},
            "response": {"status": response.status_code, "json": json.loads(body)},
        }
        self.interactions.append(record)
        return httpx.Response(response.status_code, content=body, request=request)

    def dump(self, path: Path, *, provider: str) -> None:
        path.write_text(
            json.dumps({"provider": provider, "interactions": self.interactions}, indent=2),
            encoding="utf-8",
        )


def load_cassette(name: str) -> list[dict[str, Any]]:
    """Load a committed cassette's interactions by base name (no extension)."""
    data = json.loads((CASSETTE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    interactions: list[dict[str, Any]] = data["interactions"]
    return interactions


def cassette_client(name: str) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that replays the named cassette (keyless, no net)."""
    return httpx.AsyncClient(transport=CassetteReplayTransport(load_cassette(name)))


__all__ = [
    "CASSETTE_DIR",
    "CassetteReplayTransport",
    "RecordingTransport",
    "cassette_client",
    "load_cassette",
]
