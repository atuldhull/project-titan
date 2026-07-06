"""Public builders for the egress subpackage (Phase 23).

The forward proxy and the fetch-gateway are assembled here from settings +
backing clients, so the entrypoint (``__main__``) and the tests share one
construction path."""

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.tools.egress.allowlist import Allowlist
from titan.tools.egress.cache import ResponseCache
from titan.tools.egress.forward_proxy import ForwardProxy
from titan.tools.egress.gateway import build_gateway_app
from titan.tools.egress.search import SearchBackend, build_search_backend
from titan.tools.egress.tokens import TokenClaims, mint_task_token, verify_task_token


def build_forward_proxy(
    engine: AsyncEngine, *, secret: str, mirror_hosts: frozenset[str] = frozenset()
) -> ForwardProxy:
    return ForwardProxy(
        secret=secret, allowlist=Allowlist(mirror_hosts=mirror_hosts), engine=engine
    )


def build_gateway(
    *,
    client: httpx.AsyncClient,
    internal_token: str,
    search_backend: SearchBackend | None,
    pypi_upstream: str,
    npm_upstream: str,
    cache: ResponseCache | None = None,
) -> FastAPI:
    return build_gateway_app(
        client=client,
        cache=cache or ResponseCache(),
        search_backend=search_backend,
        internal_token=internal_token,
        pypi_upstream=pypi_upstream,
        npm_upstream=npm_upstream,
    )


__all__ = [
    "Allowlist",
    "ForwardProxy",
    "ResponseCache",
    "SearchBackend",
    "TokenClaims",
    "build_forward_proxy",
    "build_gateway",
    "build_search_backend",
    "mint_task_token",
    "verify_task_token",
]
