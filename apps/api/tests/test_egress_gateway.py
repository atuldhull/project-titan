"""Hermetic tests for the caching fetch-gateway (Phase 23) — no Docker, no
internet. The gateway app runs in-process (httpx ASGITransport) and its outbound
client is an httpx MockTransport, so we assert the cache-hit / search / mirror /
SSRF / auth behavior deterministically.

Proves the DoD's research half: a repeated web.fetch is served as a cache HIT."""

from typing import Any

import httpx

from titan.tools.egress.cache import ResponseCache
from titan.tools.egress.search import SearxngBackend
from titan.tools.egress.service import build_gateway

from .conftest import run_async

_TOKEN = "internal-secret"
_HTML = "<html><head><title>Fixture</title></head><body><article>hi</article></body></html>"
_SEARXNG_JSON = {
    "results": [
        {"title": "First", "url": "https://a.test/1", "content": "snippet one"},
        {"title": "Second", "url": "https://b.test/2", "content": "snippet two"},
    ]
}


class _Upstream:
    """A MockTransport that counts calls per URL, so cache hits are observable."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls[url] = self.calls.get(url, 0) + 1
        if "/search" in url:
            return httpx.Response(200, json=_SEARXNG_JSON)
        if "pypi" in url or "/simple/" in url:
            return httpx.Response(
                200, text="<a href='x'>pkg</a>", headers={"content-type": "text/html"}
            )
        return httpx.Response(200, text=_HTML, headers={"content-type": "text/html; charset=utf-8"})


def _client_pair() -> tuple[httpx.AsyncClient, _Upstream]:
    upstream = _Upstream()
    outbound = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    return outbound, upstream


def _gateway(outbound: httpx.AsyncClient) -> Any:
    backend = SearxngBackend("http://searxng.test", outbound)
    app = build_gateway(
        client=outbound,
        internal_token=_TOKEN,
        search_backend=backend,
        pypi_upstream="https://pypi.org",
        npm_upstream="https://registry.npmjs.org",
        cache=ResponseCache(),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


_AUTH = {"authorization": f"Bearer {_TOKEN}"}


def test_fetch_caches_repeated_request() -> None:
    async def scenario() -> None:
        outbound, upstream = _client_pair()
        async with _gateway(outbound) as gw:
            first = await gw.post(
                "/fetch", json={"url": "https://example.test/page"}, headers=_AUTH
            )
            assert first.status_code == 200
            assert first.json()["from_cache"] is False
            second = await gw.post(
                "/fetch", json={"url": "https://example.test/page"}, headers=_AUTH
            )
            assert second.json()["from_cache"] is True  # DoD: repeated fetch is a cache HIT
            # The upstream was hit exactly ONCE despite two fetches.
            assert upstream.calls["https://example.test/page"] == 1
        await outbound.aclose()

    run_async(scenario)


def test_fetch_requires_the_internal_bearer() -> None:
    async def scenario() -> None:
        outbound, _ = _client_pair()
        async with _gateway(outbound) as gw:
            resp = await gw.post("/fetch", json={"url": "https://example.test/"})
            assert resp.status_code == 401
        await outbound.aclose()

    run_async(scenario)


def test_fetch_blocks_ssrf_targets() -> None:
    async def scenario() -> None:
        outbound, upstream = _client_pair()
        async with _gateway(outbound) as gw:
            for bad in ["http://169.254.169.254/latest/", "http://127.0.0.1/x", "ftp://x/y"]:
                resp = await gw.post("/fetch", json={"url": bad}, headers=_AUTH)
                assert resp.status_code in (400, 403), bad
            assert not upstream.calls  # never dialled out
        await outbound.aclose()

    run_async(scenario)


def test_fetch_blocks_redirect_to_internal() -> None:
    # SEC-074 per-hop: a public URL that 302-redirects to a metadata IP is
    # blocked at the redirect hop, and the internal host is NEVER contacted.
    async def scenario() -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if "redirector" in str(request.url):
                return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
            return httpx.Response(200, text=_HTML)

        outbound = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with _gateway(outbound) as gw:
            resp = await gw.post("/fetch", json={"url": "http://redirector.test/go"}, headers=_AUTH)
            assert resp.status_code == 403
            # The redirector was dialled once; the metadata host never was.
            assert calls == ["http://redirector.test/go"]
            assert not any("169.254" in c for c in calls)
        await outbound.aclose()

    run_async(scenario)


def test_fetch_follows_safe_redirect() -> None:
    # The per-hop guard must still FOLLOW a redirect to a benign host.
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "start" in str(request.url):
                return httpx.Response(302, headers={"location": "http://dest.test/page"})
            return httpx.Response(200, text=_HTML, headers={"content-type": "text/html"})

        outbound = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with _gateway(outbound) as gw:
            resp = await gw.post("/fetch", json={"url": "http://start.test/go"}, headers=_AUTH)
            assert resp.status_code == 200
            assert "hi" in resp.json()["body"]
        await outbound.aclose()

    run_async(scenario)


def test_search_parses_and_caches() -> None:
    async def scenario() -> None:
        outbound, upstream = _client_pair()
        async with _gateway(outbound) as gw:
            resp = await gw.get("/search", params={"q": "titan", "limit": 5}, headers=_AUTH)
            body = resp.json()
            assert body["from_cache"] is False
            assert [r["title"] for r in body["results"]] == ["First", "Second"]
            again = await gw.get("/search", params={"q": "titan", "limit": 5}, headers=_AUTH)
            assert again.json()["from_cache"] is True
        await outbound.aclose()

    run_async(scenario)


def test_resolves_to_blocked_catches_dns_to_private_ip(monkeypatch: Any) -> None:
    # The CRITICAL SSRF fix: a benign-looking DNS name that RESOLVES to a
    # metadata/private IP is blocked (host_is_blocked alone never resolves DNS).
    import socket as _socket

    from titan.tools.egress import gateway

    def fake_getaddrinfo(host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        if host == "sneaky.test":
            return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]
        raise _socket.gaierror("unresolvable")

    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)

    async def scenario() -> None:
        assert await gateway._resolves_to_blocked("sneaky.test", 80) is True  # DNS → metadata
        assert await gateway._resolves_to_blocked("127.0.0.1", 80) is True  # literal loopback
        assert await gateway._resolves_to_blocked("nonexistent.invalid", 80) is False  # gaierror

    run_async(scenario)


def test_pypi_mirror_pull_through_caches() -> None:
    async def scenario() -> None:
        outbound, upstream = _client_pair()
        async with _gateway(outbound) as gw:
            first = await gw.get("/pypi/simple/six/")
            assert first.status_code == 200 and first.headers["x-titan-cache"] == "MISS"
            second = await gw.get("/pypi/simple/six/")
            assert second.headers["x-titan-cache"] == "HIT"
            assert upstream.calls["https://pypi.org/simple/six/"] == 1
        await outbound.aclose()

    run_async(scenario)
