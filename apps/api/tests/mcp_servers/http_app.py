"""A Streamable-HTTP MCP server (Vol 7 §7) as an in-process ASGI app, so the HTTP
transport is exercised without a real network. It reuses :func:`reference_stdio.handle`,
and can optionally: require an OAuth 2.1 bearer token (proving the client sends the token
resolved from the secret store), reply as an SSE stream, negotiate a downgraded protocol
version, and inject a decoy server→client request to prove it is not mis-read as a reply.
The per-request ``MCP-Protocol-Version`` header is captured for assertions."""

import json
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tests.mcp_servers.reference_stdio import handle, make_state


def build_http_app(
    *,
    require_token: str | None = None,
    sse: bool = False,
    negotiate_version: str | None = None,
    inject_server_request: bool = False,
) -> Starlette:
    state = make_state()
    captured: dict[str, list[str]] = {"protocol_versions": []}

    async def endpoint(request: Request) -> Response:
        if request.method == "DELETE":  # session teardown
            return Response(status_code=200)
        if require_token is not None and (
            request.headers.get("authorization", "") != f"Bearer {require_token}"
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        captured["protocol_versions"].append(request.headers.get("mcp-protocol-version", ""))
        message: dict[str, Any] = await request.json()
        response = handle(message, state)
        if negotiate_version is not None and message.get("method") == "initialize" and response:
            response["result"]["protocolVersion"] = negotiate_version
        if response is None:
            return Response(status_code=202)  # a notification — accepted, no body
        headers = {"mcp-session-id": "test-session"}
        if not sse:
            return JSONResponse(response, headers=headers)
        # SSE: emit (optionally) a decoy server→client request sharing our id, then any
        # queued notifications (e.g. list_changed from 'grow'), then the real response.
        frames: list[dict[str, Any]] = []
        if inject_server_request and message.get("method") == "tools/call":
            frames.append({"jsonrpc": "2.0", "id": message.get("id"), "method": "ping"})
        frames.extend(state["notifications"])
        state["notifications"] = []
        frames.append(response)
        body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        return Response(body, media_type="text/event-stream", headers=headers)

    app = Starlette(routes=[Route("/mcp", endpoint, methods=["POST", "DELETE"])])
    app.state.captured = captured
    return app
