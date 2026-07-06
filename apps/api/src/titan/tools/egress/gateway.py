"""The caching fetch-gateway (Phase 23; Vol 7 §5 research proxy + registry mirror).

A FastAPI app the TRUSTED tool host's web.* tools call, and the caching pypi/npm
pull-through mirror the sandbox's pip/npm are pointed at. It is the only thing
that opens outbound web connections for research:

* ``POST /fetch {url}`` — fetches server-side (so the plaintext response can be
  cached with no TLS MITM), re-checks the target against the SSRF host block
  AFTER redirects, and returns ``from_cache`` so a repeated fetch is a cache hit.
* ``GET /search`` — runs the configured (SearXNG) backend, cached.
* ``GET /pypi/{path}`` / ``GET /npm/{path}`` — caching pull-through of the real
  indexes; package artifacts on the CDN are fetched by pip/npm through the
  audited forward proxy, so every byte still traverses the sole egress path.

``/fetch`` and ``/search`` require the internal bearer (the tool host); the mirror
routes are only reachable through the forward proxy, which already authenticated
and allowlisted the sandbox.
"""

import asyncio
import hmac
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog
from fastapi import FastAPI, Header, HTTPException, Request

from titan.tools.constraints import host_is_blocked
from titan.tools.egress.cache import CachedResponse, ResponseCache
from titan.tools.egress.search import SearchBackend

logger = structlog.get_logger("titan.tools.egress.gateway")

_MAX_BODY_BYTES = 8 * 1024 * 1024


async def _resolves_to_blocked(host: str, port: int) -> bool:
    """True if ``host`` (a name OR literal) resolves to ANY private/loopback/
    link-local/metadata IP — the post-DNS SSRF check the tool-side literal-IP
    guard defers to the proxy (SEC-074). Blocks the 'DNS name → 169.254.169.254'
    attack the constraint layer can't see. (A residual DNS-rebinding window
    remains between this check and httpx's own resolution; the deny-all sandbox
    never reaches this path — it CONNECT-tunnels through the forward proxy — so
    this guards only the trusted tool-host research fetch.)"""
    if host_is_blocked(host):  # literal IP / known metadata name — cheap first pass
        return True
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port or None, proto=socket.IPPROTO_TCP
        )
    except (socket.gaierror, OSError):
        return False  # unresolvable → let the fetch fail naturally
    return any(host_is_blocked(info[4][0]) for info in infos)


def build_gateway_app(
    *,
    client: httpx.AsyncClient,
    cache: ResponseCache,
    search_backend: SearchBackend | None,
    internal_token: str,
    pypi_upstream: str,
    npm_upstream: str,
) -> FastAPI:
    app = FastAPI(title="titan-egress-gateway", docs_url=None, redoc_url=None, openapi_url=None)
    pypi = pypi_upstream.rstrip("/")
    npm = npm_upstream.rstrip("/")

    def _authorize(authorization: str | None) -> None:
        # Fail closed: a blank configured token means the gateway is not wired for
        # tool access and must reject, never accept, unauthenticated callers.
        # Constant-time compare (like the token layer) — no early-exit timing oracle.
        if not internal_token or authorization is None:
            raise HTTPException(status_code=401, detail="invalid gateway credential")
        if not hmac.compare_digest(authorization, f"Bearer {internal_token}"):
            raise HTTPException(status_code=401, detail="invalid gateway credential")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/fetch")
    async def fetch(
        request: Request, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _authorize(authorization)
        payload = await request.json()
        url = str(payload.get("url") or "").strip()
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise HTTPException(status_code=400, detail="only http(s) URLs may be fetched")
        default_port = 443 if parts.scheme == "https" else 80
        # SSRF: block targets that RESOLVE to a private/metadata IP (not just
        # literal-IP URLs) — the post-DNS check the constraint layer defers here.
        if await _resolves_to_blocked(parts.hostname, parts.port or default_port):
            raise HTTPException(status_code=403, detail="target is a private/metadata address")
        cached = cache.get(url)
        if cached is not None:
            return _fetch_body(cached, from_cache=True)
        try:
            response = await client.get(url, follow_redirects=True, timeout=20.0)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"fetch failed: {exc}") from exc
        # Re-check the FINAL host after redirects (SSRF via redirect, SEC-074) —
        # resolving it too, so a redirect to a DNS name over a metadata IP is caught.
        final_host = response.url.host
        if final_host and await _resolves_to_blocked(final_host, response.url.port or 0):
            raise HTTPException(status_code=403, detail="redirected to a private/metadata address")
        body = response.content[:_MAX_BODY_BYTES]
        entry = CachedResponse(
            status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            body=body,
            final_url=str(response.url),
        )
        # Only cache success — never serve a stale transient 4xx/5xx as a cache hit.
        if response.status_code < 400:
            cache.put(url, entry)
        return _fetch_body(entry, from_cache=False)

    @app.get("/search")
    async def search(
        q: str, limit: int = 8, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        _authorize(authorization)
        if search_backend is None:
            raise HTTPException(status_code=503, detail="search backend is disabled")
        limit = max(1, min(limit, 25))
        key = f"search:{limit}:{q}"
        cached = cache.get(key)
        if cached is not None:
            return {"query": q, "from_cache": True, **_json_body(cached)}
        try:
            results = await search_backend.search(q, limit=limit)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"search failed: {exc}") from exc
        payload = {
            "results": [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results]
        }
        cache.put(key, _encode_json(payload))
        return {"query": q, "from_cache": False, **payload}

    @app.get("/pypi/{path:path}")
    async def pypi_mirror(path: str) -> Any:
        return await _pull_through(client, cache, f"{pypi}/{path}")

    @app.get("/npm/{path:path}")
    async def npm_mirror(path: str) -> Any:
        return await _pull_through(client, cache, f"{npm}/{path}")

    return app


def _fetch_body(entry: CachedResponse, *, from_cache: bool) -> dict[str, Any]:
    return {
        "url": entry.final_url,
        "final_url": entry.final_url,
        "status": entry.status,
        "content_type": entry.content_type,
        "from_cache": from_cache,
        "body": entry.body.decode("utf-8", "replace"),
    }


def _encode_json(payload: dict[str, Any]) -> CachedResponse:
    import json

    return CachedResponse(
        status=200,
        content_type="application/json",
        body=json.dumps(payload).encode("utf-8"),
        final_url="",
    )


def _json_body(entry: CachedResponse) -> dict[str, Any]:
    import json

    data: dict[str, Any] = json.loads(entry.body.decode("utf-8"))
    return data


async def _pull_through(client: httpx.AsyncClient, cache: ResponseCache, upstream_url: str) -> Any:
    from fastapi import Response

    cached = cache.get(upstream_url)
    if cached is not None:
        return Response(
            content=cached.body,
            status_code=cached.status,
            media_type=cached.content_type or "application/octet-stream",
            headers={"x-titan-cache": "HIT"},
        )
    try:
        response = await client.get(upstream_url, follow_redirects=True, timeout=30.0)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"mirror fetch failed: {exc}") from exc
    # Registry metadata must be served WHOLE — a truncated packument/index is
    # invalid JSON/HTML and breaks the install. Serve the full body; if it
    # exceeds the cap, do NOT poison the cache with a partial copy.
    body = response.content
    content_type = response.headers.get("content-type", "application/octet-stream")
    if response.status_code == 200 and len(body) <= _MAX_BODY_BYTES:
        cache.put(
            upstream_url,
            CachedResponse(
                status=200, content_type=content_type, body=body, final_url=str(response.url)
            ),
        )
    return Response(
        content=body,
        status_code=response.status_code,
        media_type=content_type,
        headers={"x-titan-cache": "MISS"},
    )


__all__ = ["build_gateway_app"]
