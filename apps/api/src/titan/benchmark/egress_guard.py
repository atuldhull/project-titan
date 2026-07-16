"""Egress guard for the offline acceptance run (TITAN-FND-054, Phase 56).

A real passthrough httpx transport that RECORDS every host contacted and enforces
an **allowlist** — only loopback and the configured Ollama host may be reached;
anything else raises. Injected into the LLM gateway via
``build_gateway(..., client=...)`` so it covers the exact calls that matter — the
provider requests the adapters make directly.

An allowlist (not a denylist of known paid hosts) is what actually proves the
``offline`` claim: a future config or connector pointing at *any* remote host —
Azure OpenAI, Bedrock, an OpenAI-compatible proxy — is blocked and marks the run
non-clean, rather than slipping through because it wasn't on a hand-maintained
denylist.
"""

from urllib.parse import urlsplit

import httpx

#: Hosts always allowed (the local machine).
_LOOPBACK: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


class EgressBlocked(RuntimeError):
    """Raised when a request targets a host outside the offline allowlist."""


def _host_of(base_url: str | None) -> str | None:
    if not base_url:
        return None
    host = urlsplit(base_url).hostname
    return host.lower() if host else None


class EgressGuard(httpx.AsyncBaseTransport):
    """Wraps a real transport; records contacted hosts, allows only loopback +
    the configured Ollama host, blocks (and records) everything else."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        *,
        allow_hosts: set[str] | None = None,
        ollama_base_url: str | None = None,
    ) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._allow = set(_LOOPBACK)
        self._allow.update(h.lower() for h in (allow_hosts or set()))
        ollama_host = _host_of(ollama_base_url)
        if ollama_host:
            self._allow.add(ollama_host)
        self.hosts_contacted: set[str] = set()
        self.blocked: list[str] = []

    def _allowed(self, host: str) -> bool:
        return host in self._allow

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = (request.url.host or "").lower()
        self.hosts_contacted.add(host)
        if not self._allowed(host):
            self.blocked.append(host)
            raise EgressBlocked(
                f"off-allowlist egress blocked during offline run: {host} "
                f"(allowed: {sorted(self._allow)})"
            )
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()

    @property
    def clean(self) -> bool:
        """True iff every contacted host was on the offline allowlist."""
        return not self.blocked and all(self._allowed(h) for h in self.hosts_contacted)


def guarded_client(guard: EgressGuard, *, timeout: float = 600.0) -> httpx.AsyncClient:
    """An httpx client routed through ``guard`` (long timeout — local turns are slow)."""
    return httpx.AsyncClient(transport=guard, timeout=timeout)


__all__ = ["EgressBlocked", "EgressGuard", "guarded_client"]
