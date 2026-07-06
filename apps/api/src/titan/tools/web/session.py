"""The web tools' client to the caching fetch-gateway (Phase 23).

``web.*`` tools never open direct connections — they go through the gateway
(``titan.tools.egress``) via :class:`ResearchClient`, built once at boot, and a
per-call :class:`ResearchSession` handed to the executor on
``ToolExecutionContext.research`` (mirroring the Dind/Browser session pattern).
Absent a configured gateway the session is None and web.* fail closed
(TOOL_UNAVAILABLE)."""

from typing import Any

import httpx

from titan.tools.errors import ToolError


class ResearchClient:
    """Boot-time client to the fetch-gateway (research fetch + search)."""

    def __init__(self, http: httpx.AsyncClient, *, gateway_url: str, token: str) -> None:
        self._http = http
        self._base = gateway_url.rstrip("/")
        self._headers = {"authorization": f"Bearer {token}"} if token else {}

    async def fetch(self, url: str) -> dict[str, Any]:
        response = await self._request("POST", "/fetch", json={"url": url})
        return response

    async def search(self, query: str, *, limit: int) -> dict[str, Any]:
        return await self._request("GET", "/search", params={"q": query, "limit": limit})

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(
                method, f"{self._base}{path}", headers=self._headers, timeout=30.0, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ToolError("UPSTREAM_ERROR", f"research gateway unreachable: {exc}") from exc
        if response.status_code == 403:
            raise ToolError("SANDBOX_VIOLATION", _detail(response))
        if response.status_code in (401, 503):
            raise ToolError("TOOL_UNAVAILABLE", _detail(response))
        if response.status_code >= 400:
            raise ToolError("UPSTREAM_ERROR", _detail(response))
        data: dict[str, Any] = response.json()
        return data

    async def aclose(self) -> None:
        await self._http.aclose()


class ResearchSession:
    """The per-call handle placed in ``ToolExecutionContext.research``."""

    def __init__(self, client: ResearchClient) -> None:
        self._client = client

    async def fetch(self, url: str) -> dict[str, Any]:
        return await self._client.fetch(url)

    async def search(self, query: str, *, limit: int) -> dict[str, Any]:
        return await self._client.search(query, limit=limit)


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        detail = payload.get("detail") if isinstance(payload, dict) else None
        return str(detail or f"gateway returned {response.status_code}")
    except ValueError:
        return f"gateway returned {response.status_code}"


__all__ = ["ResearchClient", "ResearchSession"]
