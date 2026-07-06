"""Hermetic tests for the forward proxy (Phase 23) — real loopback sockets, no
Docker, no internet. A tiny asyncio origin stands in for an allowlisted host; the
proxy is driven with httpx's proxy support. ``schema``-marked because the deny
path writes a SANDBOX_VIOLATION row to the real audit ledger.

Proves the DoD's egress half: a request to a non-allowlisted host FAILS (403) and
is AUDITED, while an allowlisted host is forwarded and auth is enforced."""

import asyncio
import contextlib
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from titan.tools.egress.service import build_forward_proxy
from titan.tools.egress.tokens import mint_task_token

from .conftest import run_async

pytestmark = pytest.mark.schema

_SECRET = "proxy-secret"


async def _start_origin() -> tuple[asyncio.Server, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(asyncio.IncompleteReadError):
            await reader.readuntil(b"\r\n\r\n")
        body = b"origin-ok"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (len(body), body)
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _start_proxy(engine: Any) -> tuple[asyncio.Server, int]:
    # Allow the loopback fixture host so the "allow" path has a real upstream.
    proxy = build_forward_proxy(engine, secret=_SECRET, mirror_hosts=frozenset({"127.0.0.1"}))
    server = await proxy.serve("127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _token(project_id: uuid.UUID) -> str:
    return mint_task_token(_SECRET, project_id=project_id, task_id=uuid.uuid4(), agent_id="sandbox")


async def _audit_rows(engine: Any, project_id: uuid.UUID) -> list[Any]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT tool, error_code, status FROM tool_audit WHERE project_id = :p"),
            {"p": project_id},
        )
        return list(result)


def test_allowlisted_host_is_forwarded(migrated_engine: Any) -> None:
    async def scenario() -> None:
        origin, origin_port = await _start_origin()
        proxy, proxy_port = await _start_proxy(migrated_engine)
        token = _token(uuid.uuid4())
        try:
            async with httpx.AsyncClient(
                proxy=f"http://task:{token}@127.0.0.1:{proxy_port}", timeout=10.0
            ) as client:
                resp = await client.get(f"http://127.0.0.1:{origin_port}/")
                assert resp.status_code == 200 and resp.text == "origin-ok"
        finally:
            origin.close()
            proxy.close()

    run_async(scenario)


def test_denied_host_is_403_and_audited(migrated_engine: Any) -> None:
    async def scenario() -> None:
        proxy, proxy_port = await _start_proxy(migrated_engine)
        project_id = uuid.uuid4()
        token = _token(project_id)
        try:
            async with httpx.AsyncClient(
                proxy=f"http://task:{token}@127.0.0.1:{proxy_port}", timeout=10.0
            ) as client:
                # example.com is not allowlisted → forwarded HTTP gets a 403 body.
                resp = await client.get("http://example.com/")
                assert resp.status_code == 403
            rows = await _audit_rows(migrated_engine, project_id)
            assert any(
                r.tool == "net.egress" and r.error_code == "SANDBOX_VIOLATION" for r in rows
            ), rows
        finally:
            proxy.close()

    run_async(scenario)


def test_missing_or_bad_credential_is_407(migrated_engine: Any) -> None:
    async def scenario() -> None:
        proxy, proxy_port = await _start_proxy(migrated_engine)
        try:
            # No credential.
            async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{proxy_port}", timeout=10.0) as c:
                resp = await c.get("http://example.com/")
                assert resp.status_code == 407
            # A bad/forged token.
            async with httpx.AsyncClient(
                proxy=f"http://task:garbage@127.0.0.1:{proxy_port}", timeout=10.0
            ) as c2:
                resp2 = await c2.get("http://example.com/")
                assert resp2.status_code == 407
        finally:
            proxy.close()

    run_async(scenario)


def test_connect_to_denied_host_fails_and_is_audited(migrated_engine: Any) -> None:
    async def scenario() -> None:
        proxy, proxy_port = await _start_proxy(migrated_engine)
        project_id = uuid.uuid4()
        token = _token(project_id)
        try:
            async with httpx.AsyncClient(
                proxy=f"http://task:{token}@127.0.0.1:{proxy_port}", timeout=10.0
            ) as client:
                # An HTTPS CONNECT to a denied host is refused by the proxy.
                with pytest.raises(httpx.HTTPError):
                    await client.get("https://example.com/")
            rows = await _audit_rows(migrated_engine, project_id)
            assert any(r.error_code == "SANDBOX_VIOLATION" for r in rows), rows
        finally:
            proxy.close()

    run_async(scenario)
