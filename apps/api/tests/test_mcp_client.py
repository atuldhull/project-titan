"""The MCP client transports + protocol layer (Phase 53; Vol 7 §7, TITAN-INT-050, R3).

No database: these drive the client directly against the deterministic reference servers
(a stdio subprocess and an in-process Streamable-HTTP ASGI app). They prove the handshake,
tool/resource/prompt discovery, tools/call normalization, protocol-version pinning, fault
isolation, and that the HTTP transport carries the OAuth bearer token from the secret store.
"""

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import run_async
from tests.mcp_servers.http_app import build_http_app
from titan.plugins.jsonrpc import RpcError
from titan.plugins.mcp import (
    SUPPORTED_PROTOCOL_VERSIONS,
    HttpMcpTransport,
    McpClient,
    McpTransport,
    StdioMcpTransport,
)

_REF_SERVER = str(Path(__file__).parent / "mcp_servers" / "reference_stdio.py")


def _stdio_client() -> McpClient:
    transport = StdioMcpTransport([sys.executable, _REF_SERVER], cwd=str(Path(__file__).parent))
    return McpClient("titan.ref", transport, call_timeout_s=10.0)


# --- stdio transport (spawned subprocess, the server-filesystem shape) -------


def test_stdio_handshake_discovers_and_calls() -> None:
    async def scenario() -> dict[str, Any]:
        mcp = _stdio_client()
        await mcp.start()
        await mcp.initialize()
        tools = await mcp.list_tools()
        resources = await mcp.list_resources()
        prompts = await mcp.list_prompts()
        added = await mcp.call_tool("add", {"a": 2, "b": 3})
        note = await mcp.read_resource("mem://note")
        prompt = await mcp.get_prompt("greet", {"who": "titan"})
        version = mcp.protocol_version
        await mcp.stop()
        return {
            "version": version,
            "tools": sorted(t.name for t in tools),
            "resources": [r.uri for r in resources],
            "prompts": [p.name for p in prompts],
            "added": added,
            "note": note,
            "prompt": prompt,
        }

    out = run_async(scenario)
    assert out["version"] in SUPPORTED_PROTOCOL_VERSIONS
    assert {"add", "boom", "echo", "grow", "tool_error"} <= set(out["tools"])
    assert out["resources"] == ["mem://note"]
    assert out["prompts"] == ["greet"]
    assert out["added"]["structuredContent"] == {"sum": 5.0}
    assert out["note"]["contents"][0]["text"] == "note body"
    assert "hi titan" in out["prompt"]["messages"][0]["content"]["text"]


def test_stdio_tool_fault_isolates_and_counts_toward_quarantine() -> None:
    async def scenario() -> dict[str, Any]:
        mcp = _stdio_client()
        await mcp.start()
        await mcp.initialize()
        fault_code = None
        try:
            await mcp.call_tool("boom", {})
        except RpcError as exc:
            fault_code = exc.code
        after_fault = mcp.consecutive_errors
        # A tool-level error (isError=true) is a NORMAL result, not a transport fault.
        tool_err = await mcp.call_tool("tool_error", {})
        after_tool_error = mcp.consecutive_errors
        await mcp.stop()
        return {
            "fault_code": fault_code,
            "after_fault": after_fault,
            "tool_err": tool_err,
            "after_tool_error": after_tool_error,
        }

    out = run_async(scenario)
    assert out["fault_code"] == -32603  # INTERNAL_ERROR from the server
    assert out["after_fault"] == 1  # a genuine fault advances the streak
    assert out["tool_err"]["isError"] is True
    assert out["after_tool_error"] == 0  # isError did NOT — and it reset the streak


# --- protocol-version pinning + notification dispatch (fake transport) -------


class _FakeTransport:
    """A canned-response transport for the parts that need no subprocess (R3 version
    pinning, notification routing)."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.on_notification: Any = None
        self.notified: list[tuple[str, dict[str, Any]]] = []
        self.negotiated: str = ""
        self.alive = True

    async def start(self) -> None: ...

    async def send_request(self, method: str, params: dict[str, Any], *, timeout_s: float) -> Any:
        value = self._responses.get(method, {})
        if isinstance(value, RpcError):
            raise value
        return value

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        self.notified.append((method, params))

    def set_protocol_version(self, version: str) -> None:
        self.negotiated = version

    async def stop(self) -> None:
        self.alive = False


@pytest.mark.parametrize(
    "init_result",
    [
        {"protocolVersion": "1999-01-01", "capabilities": {}, "serverInfo": {}},  # unknown
        {"capabilities": {}, "serverInfo": {}},  # MISSING protocolVersion (spec violation)
        {},  # empty
    ],
)
def test_initialize_refuses_a_bad_protocol_version(init_result: dict[str, Any]) -> None:
    transport: McpTransport = _FakeTransport({"initialize": init_result})
    mcp = McpClient("titan.x", transport)

    async def scenario() -> int:
        await mcp.start()
        with pytest.raises(RpcError) as exc:
            await mcp.initialize()
        return exc.value.code

    # An unsupported OR missing/empty version is refused BEFORE the initialized ack (R3).
    assert run_async(scenario) != 0
    assert transport.notified == []  # type: ignore[attr-defined]


def test_initialize_acks_and_records_the_negotiated_version() -> None:
    # A server may negotiate DOWN to an older supported version; the client accepts it,
    # records it, sends notifications/initialized, and pins it on the transport.
    transport = _FakeTransport(
        {"initialize": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {}}}
    )
    mcp = McpClient("titan.x", transport)

    async def scenario() -> None:
        await mcp.start()
        await mcp.initialize()

    run_async(scenario)
    assert mcp.protocol_version == "2025-03-26"
    assert transport.notified == [("notifications/initialized", {})]
    assert transport.negotiated == "2025-03-26"  # propagated to the transport (HTTP header)


def test_pagination_follows_next_cursor_and_bounds() -> None:
    # A paginating tools/list: page 1 has a cursor, page 2 terminates.
    class _Paging(_FakeTransport):
        def __init__(self) -> None:
            super().__init__({})
            self._page = 0

        async def send_request(
            self, method: str, params: dict[str, Any], *, timeout_s: float
        ) -> Any:
            if method != "tools/list":
                return {}
            self._page += 1
            if self._page == 1:
                return {"tools": [{"name": "a", "inputSchema": {}}], "nextCursor": "c1"}
            return {"tools": [{"name": "b", "inputSchema": {}}]}

    mcp = McpClient("titan.x", _Paging())

    async def scenario() -> list[str]:
        tools = await mcp.list_tools()
        return [t.name for t in tools]

    assert run_async(scenario) == ["a", "b"]


def test_tools_list_changed_notification_invokes_the_handler() -> None:
    transport = _FakeTransport({})
    mcp = McpClient("titan.x", transport)
    fired: list[str] = []

    async def handler() -> None:
        fired.append("reregister")

    mcp.set_tools_changed_handler(handler)

    async def scenario() -> list[str]:
        await mcp._handle_notification("notifications/tools/list_changed", {})
        await mcp._handle_notification("notifications/something_else", {})
        return fired

    assert run_async(scenario) == ["reregister"]  # only list_changed triggers re-registration


# --- Streamable HTTP transport (in-process ASGI, OAuth bearer) ---------------


def _http_client_scenario(*, server_token: str | None, client_token: str) -> Any:
    async def scenario() -> dict[str, Any]:
        app = build_http_app(require_token=server_token)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://mcp.test"
        ) as client:
            transport = HttpMcpTransport(
                "http://mcp.test/mcp", client=client, bearer_token=client_token
            )
            mcp = McpClient("titan.http", transport)
            await mcp.start()
            init_error = None
            try:
                await mcp.initialize()
            except RpcError as exc:
                init_error = exc.code
                await mcp.stop()
                return {"init_error": init_error}
            tools = await mcp.list_tools()
            added = await mcp.call_tool("add", {"a": 10, "b": 5})
            await mcp.stop()
            return {"init_error": None, "tools": sorted(t.name for t in tools), "added": added}

    return run_async(scenario)


def test_http_transport_initializes_and_calls_with_a_bearer_token() -> None:
    out = _http_client_scenario(server_token="s3cret-token", client_token="s3cret-token")
    assert out["init_error"] is None
    assert "add" in out["tools"]
    assert out["added"]["structuredContent"] == {"sum": 15.0}


def test_http_transport_is_rejected_without_the_right_token() -> None:
    out = _http_client_scenario(server_token="right-token", client_token="wrong-token")
    assert out["init_error"] is not None  # 401 surfaces as a structured RpcError, not a hang


def test_http_sse_response_is_parsed_and_server_request_is_not_misparsed() -> None:
    # The server replies as text/event-stream AND injects a decoy server→client request
    # carrying the SAME id as our tools/call — the real response must still win.
    app = build_http_app(sse=True, inject_server_request=True)

    async def scenario() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://mcp.test"
        ) as client:
            transport = HttpMcpTransport("http://mcp.test/mcp", client=client)
            mcp = McpClient("titan.http", transport)
            await mcp.start()
            await mcp.initialize()
            added = await mcp.call_tool("add", {"a": 7, "b": 8})
            return added

    added = run_async(scenario)
    assert added["structuredContent"] == {"sum": 15.0}  # not the decoy ping's empty result


def test_http_negotiated_version_is_sent_on_later_requests() -> None:
    app = build_http_app(sse=False, negotiate_version="2025-03-26")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://mcp.test"
        ) as client:
            transport = HttpMcpTransport("http://mcp.test/mcp", client=client)
            mcp = McpClient("titan.http", transport)
            await mcp.start()
            await mcp.initialize()
            await mcp.list_tools()

    run_async(scenario)
    seen = app.state.captured["protocol_versions"]
    assert seen[0] == "2025-06-18"  # the initialize request proposes our pinned version
    assert seen[1:] and all(v == "2025-03-26" for v in seen[1:])  # then the negotiated one


def test_http_sse_notification_fans_out_to_the_handler() -> None:
    # 'grow' queues a list_changed notification; over SSE it rides alongside the response
    # and must reach the handler (a strongly-referenced task, not a GC-droppable one).
    app = build_http_app(sse=True)
    fired: list[str] = []

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://mcp.test"
        ) as client:
            transport = HttpMcpTransport("http://mcp.test/mcp", client=client)
            mcp = McpClient("titan.http", transport)
            mcp.set_tools_changed_handler(lambda: _append(fired))
            await mcp.start()
            await mcp.initialize()
            await mcp.call_tool("grow", {})
            for task in list(transport._tasks):  # await the fan-out task deterministically
                await task

    run_async(scenario)
    assert fired == ["changed"]


async def _append(sink: list[str]) -> None:
    sink.append("changed")
