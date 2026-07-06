"""Pluggable search backend for web.search (Phase 23; Vol 7 §5).

SearXNG (self-hosted, free) is the default; a provider-API backend is
config-swappable behind the same ``SearchBackend`` protocol. Backends run inside
the gateway (server-side), so the sandbox/tool-host never talks to a search
provider directly — only to the gateway."""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchBackend(Protocol):
    async def search(self, query: str, *, limit: int) -> list[SearchResult]: ...


class SearxngBackend:
    """Queries a self-hosted SearXNG instance's JSON API."""

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._client = client

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        response = await self._client.get(
            f"{self._base}/search",
            params={"q": query, "format": "json", "safesearch": "1"},
            timeout=15.0,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return _normalize(payload.get("results") or [], limit)


def _normalize(rows: list[dict[str, Any]], limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    for row in rows:
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        results.append(
            SearchResult(
                title=str(row.get("title") or "").strip()[:300],
                url=url,
                snippet=str(row.get("content") or "").strip()[:1000],
            )
        )
        if len(results) >= limit:
            break
    return results


def build_search_backend(
    backend: str, *, searxng_url: str, client: httpx.AsyncClient
) -> SearchBackend | None:
    """The configured backend, or None when search is disabled ('none')."""
    if backend == "searxng":
        return SearxngBackend(searxng_url, client)
    return None


__all__ = ["SearchBackend", "SearchResult", "SearxngBackend", "build_search_backend"]
