"""The MCP (Model Context Protocol) client (Vol 7 §7, TITAN-INT-050, ADR-INT-02).

An MCP server is an ORDINARY TITAN plugin: ``runtime.type: mcp`` in the manifest, no
TITAN-specific code. This module is the client half — it connects to a spec-compliant
server, performs the ``initialize`` handshake, discovers its tools/resources/prompts,
and proxies ``tools/call``. Everything TITAN-side (schema validation, the authz/risk
gate, the audit trail) is applied by the Phase-19 Tool Bus because each discovered tool
is registered as a normal :class:`~titan.tools.registry.ToolSpec`; there is no parallel
path (§4.2).

Two transports, one protocol layer:

* ``stdio`` — spawn the server's ``command`` as a subprocess and speak newline-delimited
  JSON-RPC 2.0 on its stdio (MCP's stdio transport, the SAME framing Phase 52 uses, which
  is why :mod:`titan.plugins.jsonrpc` is shared). Crash/hang isolation is identical to a
  python plugin (TITAN-INT-016); real egress containment is the §3.5 container layer.
* ``http`` — Streamable HTTP: POST JSON-RPC to the server ``url`` with an OAuth 2.1 bearer
  token from the secret store, parsing either a JSON body or an SSE stream. The endpoint
  is bound by the manifest egress allowlist (checked before connect).

The client answers the handful of server→client requests a stateless client must
(``ping`` and ``roots/list``) and dispatches ``notifications/tools/list_changed`` to a
handler so the supervisor can re-register the server's tool set live.
"""

import asyncio
import contextlib
import itertools
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import structlog

from titan.plugins import jsonrpc
from titan.plugins.jsonrpc import Request, Response, RpcError

logger = structlog.get_logger("titan.plugins.mcp")

#: The MCP revision TITAN implements + the older revisions it also accepts (R3: pin the
#: supported spec versions; a server that only speaks something else is refused loudly).
MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18", "2025-03-26", "2024-11-05"})

CLIENT_INFO = {"name": "titan", "title": "TITAN", "version": "1.0"}

# MCP method names (client→server).
M_INITIALIZE = jsonrpc.M_INITIALIZE  # "initialize"
M_TOOLS_LIST = jsonrpc.M_TOOLS_LIST  # "tools/list"
M_TOOLS_CALL = jsonrpc.M_TOOLS_CALL  # "tools/call"
M_RESOURCES_LIST = "resources/list"
M_RESOURCES_READ = "resources/read"
M_PROMPTS_LIST = "prompts/list"
M_PROMPTS_GET = "prompts/get"
M_PING = "ping"
M_ROOTS_LIST = "roots/list"

# Notifications.
N_INITIALIZED = "notifications/initialized"
N_TOOLS_CHANGED = "notifications/tools/list_changed"
N_RESOURCES_CHANGED = "notifications/resources/list_changed"
N_PROMPTS_CHANGED = "notifications/prompts/list_changed"

DEFAULT_CALL_TIMEOUT_S = 60.0
DEFAULT_HANDSHAKE_TIMEOUT_S = 20.0
_STOP_GRACE_S = 2.0
_DRAIN_TIMEOUT_S = 5.0
_STREAM_LIMIT = 16 * 1024 * 1024  # match the plugin transport's 16 MiB frame ceiling

# Only genuine transport faults advance the auto-quarantine streak (TITAN-INT-014); an
# INVALID_PARAMS / METHOD_NOT_FOUND is the caller's fault and a tool-level isError is a
# normal result, so neither may quarantine a healthy server.
_FAULT_CODES = frozenset({jsonrpc.INTERNAL_ERROR, jsonrpc.TIMEOUT, jsonrpc.PLUGIN_CRASHED})

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


# --- discovered shapes -------------------------------------------------------


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class McpResource:
    uri: str
    name: str
    description: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class McpPrompt:
    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)


# --- transport ---------------------------------------------------------------


class McpTransport(Protocol):
    """The seam the protocol layer sits on. ``on_notification`` is invoked (in its own
    task) for every server→client notification, so the read side never blocks."""

    on_notification: NotificationHandler | None

    @property
    def alive(self) -> bool: ...

    async def start(self) -> None: ...

    async def send_request(
        self, method: str, params: dict[str, Any], *, timeout_s: float
    ) -> Any: ...

    async def send_notification(self, method: str, params: dict[str, Any]) -> None: ...

    def set_protocol_version(self, version: str) -> None: ...

    async def stop(self) -> None: ...


async def _default_server_request(method: str, params: dict[str, Any]) -> Any:
    """Answer the requests a minimal MCP client must handle. We advertise no roots and no
    sampling, so ``roots/list`` is empty and everything else is METHOD_NOT_FOUND."""
    if method == M_PING:
        return {}
    if method == M_ROOTS_LIST:
        return {"roots": []}
    raise RpcError(jsonrpc.METHOD_NOT_FOUND, f"client does not implement '{method}'")


class StdioMcpTransport:
    """Spawn ``command`` and speak newline-delimited JSON-RPC on its stdio."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        on_notification: NotificationHandler | None = None,
    ) -> None:
        if not command:
            raise ValueError("stdio MCP transport requires a non-empty command")
        self._command = command
        self._cwd = cwd
        self._env = env or {}
        self.on_notification = on_notification
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._draining: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Response]] = {}
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task[None]] = set()
        self.stderr_tail: list[str] = []

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1", **self._env}
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=self._cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
        self._reader = asyncio.create_task(self._read_loop())
        self._draining = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())
                del self.stderr_tail[:-50]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — draining must never kill core
            logger.debug("mcp.stderr_drain_stopped")

    async def send_request(self, method: str, params: dict[str, Any], *, timeout_s: float) -> Any:
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise RpcError(jsonrpc.PLUGIN_CRASHED, "MCP server is not running")
        request_id = next(self._ids)
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._proc.stdin.write(jsonrpc.encode(Request(method=method, params=params, id=request_id)))
        try:
            await asyncio.wait_for(self._proc.stdin.drain(), timeout=_DRAIN_TIMEOUT_S)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(request_id, None)
            raise RpcError(jsonrpc.PLUGIN_CRASHED, "MCP server died") from exc
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise RpcError(jsonrpc.TIMEOUT, "MCP server stopped reading its input") from exc
        try:
            response = await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise RpcError(jsonrpc.TIMEOUT, f"MCP '{method}' exceeded {timeout_s:g}s") from exc
        finally:
            self._pending.pop(request_id, None)
        if response.error is not None:
            raise response.error
        return response.result

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        if not self.alive or self._proc is None or self._proc.stdin is None:
            return
        self._proc.stdin.write(jsonrpc.encode(Request(method=method, params=params, id=None)))
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
            await asyncio.wait_for(self._proc.stdin.drain(), timeout=_DRAIN_TIMEOUT_S)

    def set_protocol_version(self, version: str) -> None:
        # stdio carries no per-message version header — a no-op, present for transport parity.
        return

    async def stop(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        for task in (self._reader, self._draining, *self._tasks):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._tasks.clear()
        self._fail_pending(RpcError(jsonrpc.PLUGIN_CRASHED, "MCP server stopped"))
        self._proc = None
        self._reader = None
        self._draining = None

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    message = jsonrpc.decode(line)
                except RpcError:
                    continue  # a non-protocol line (e.g. a server log to stdout) — ignore
                if "method" in message:
                    self._spawn(self._dispatch_inbound(message))
                else:
                    self._resolve(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a reader fault must not kill core
            logger.exception("mcp.reader_error")
        finally:
            self._fail_pending(RpcError(jsonrpc.PLUGIN_CRASHED, "MCP server exited"))

    def _resolve(self, message: dict[str, Any]) -> None:
        response = jsonrpc.parse_response(message)
        if response.id is None:
            return
        future = self._pending.get(int(response.id))
        if future is not None and not future.done():
            future.set_result(response)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch_inbound(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        request_id = message.get("id")
        if request_id is None:  # a notification — no reply expected
            if self.on_notification is not None:
                with contextlib.suppress(Exception):
                    await self.on_notification(method, dict(params))
            return
        # A server→client request: answer it (deadlock-free — we are off the read loop).
        try:
            reply = Response(id=request_id, result=await _default_server_request(method, params))
        except RpcError as exc:
            reply = Response(id=request_id, error=exc)
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError, TimeoutError):
            proc.stdin.write(jsonrpc.encode(reply))
            # Bound the drain (a server that stopped reading its stdin must not leak a
            # forever-pending write task).
            await asyncio.wait_for(proc.stdin.drain(), timeout=_DRAIN_TIMEOUT_S)

    def _fail_pending(self, error: RpcError) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result(Response(id=None, error=error))
        self._pending.clear()


class HttpMcpTransport:
    """Streamable HTTP (MCP's HTTP transport): each request is one POST that returns
    either a JSON body or an SSE stream. Redirects are OFF (a 3xx to an internal address
    is an SSRF vector); the egress allowlist is enforced by the caller before connect."""

    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient,
        bearer_token: str = "",
        on_notification: NotificationHandler | None = None,
    ) -> None:
        self._url = url
        self._client = client
        self._bearer = bearer_token
        self.on_notification = on_notification
        self._ids = itertools.count(1)
        self._session_id: str | None = None
        self._closed = False
        # After the handshake, the MCP-Protocol-Version header must carry the NEGOTIATED
        # version (a server may downgrade); until then we advertise the one we propose.
        self._protocol_version = MCP_PROTOCOL_VERSION
        self.stderr_tail: list[str] = []
        # Notifications carried on a response stream are served off-thread; hold strong
        # references or a bare task can be GC'd mid-flight and the re-registration lost.
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def alive(self) -> bool:
        return not self._closed

    async def start(self) -> None:
        self._closed = False

    def set_protocol_version(self, version: str) -> None:
        if version:
            self._protocol_version = version

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": self._protocol_version,
        }
        if self._bearer:
            headers["authorization"] = f"Bearer {self._bearer}"
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def send_request(self, method: str, params: dict[str, Any], *, timeout_s: float) -> Any:
        request_id = next(self._ids)
        body = Request(method=method, params=params, id=request_id).to_dict()
        try:
            response = await self._client.post(
                self._url,
                json=body,
                headers=self._headers(),
                follow_redirects=False,
                timeout=timeout_s,
            )
        except httpx.HTTPError as exc:
            raise RpcError(jsonrpc.PLUGIN_CRASHED, f"MCP HTTP transport error: {exc}") from exc
        session = response.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        if response.status_code >= 400:
            # 401/403 is an auth/permission condition (bad or missing bearer token), NOT a
            # server malfunction — surface it as PERMISSION_DENIED so a token problem does
            # not count toward the 3-strike auto-quarantine (only true faults should).
            code = (
                jsonrpc.PERMISSION_DENIED
                if response.status_code in (401, 403)
                else jsonrpc.INTERNAL_ERROR
            )
            raise RpcError(code, f"MCP server returned HTTP {response.status_code}")
        matched = self._extract(response, request_id)
        if matched is None:
            raise RpcError(jsonrpc.INTERNAL_ERROR, f"no MCP response for '{method}'")
        if matched.error is not None:
            raise matched.error
        return matched.result

    def _extract(self, response: httpx.Response, request_id: int) -> Response | None:
        """Pull the matching JSON-RPC response out of a JSON body or an SSE stream, routing
        any server notifications carried alongside it to ``on_notification``."""
        content_type = response.headers.get("content-type", "")
        messages: list[dict[str, Any]] = []
        if content_type.startswith("text/event-stream"):
            for event in _iter_sse(response.text):
                with contextlib.suppress(json.JSONDecodeError):
                    messages.append(json.loads(event))
        else:
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(response.text)
                messages = payload if isinstance(payload, list) else [payload]
        answer: Response | None = None
        for message in messages:
            if not isinstance(message, dict):
                continue
            if "method" in message:
                # A notification (no id) → fan out. A server→client REQUEST (method + id)
                # cannot be answered on this one-shot POST response and we advertise no
                # sampling — ignore it rather than let it be mis-read as our reply.
                if message.get("id") is None:
                    self._fanout_notification(message)
                continue
            # Our response: match the id, tolerating a server that echoes it as a string.
            if message.get("id") == request_id or str(message.get("id")) == str(request_id):
                answer = jsonrpc.parse_response(message)
        return answer

    def _fanout_notification(self, message: dict[str, Any]) -> None:
        if self.on_notification is None:
            return
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        task = asyncio.ensure_future(self.on_notification(method, dict(params)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        body = Request(method=method, params=params, id=None).to_dict()
        with contextlib.suppress(httpx.HTTPError):
            await self._client.post(
                self._url, json=body, headers=self._headers(), follow_redirects=False, timeout=10.0
            )

    async def stop(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._session_id:
            with contextlib.suppress(httpx.HTTPError):
                # follow_redirects=False for parity with post/notification: never forward the
                # bearer token on a 3xx during session teardown.
                await self._client.request(
                    "DELETE",
                    self._url,
                    headers=self._headers(),
                    follow_redirects=False,
                    timeout=5.0,
                )


def _iter_sse(text: str) -> list[str]:
    """The ``data:`` payloads of an SSE body, one per event (blank-line delimited)."""
    events: list[str] = []
    buffer: list[str] = []
    for raw in text.splitlines():
        if raw == "":
            if buffer:
                events.append("\n".join(buffer))
                buffer = []
            continue
        if raw.startswith("data:"):
            buffer.append(raw[5:].lstrip())
    if buffer:
        events.append("\n".join(buffer))
    return events


# --- protocol layer ----------------------------------------------------------


class McpClient:
    """One connection to an MCP server. Uniform with :class:`PluginProcess` where the
    supervisor touches it: ``consecutive_errors``, ``health``, ``stop``."""

    def __init__(
        self,
        plugin_id: str,
        transport: McpTransport,
        *,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    ) -> None:
        self.plugin_id = plugin_id
        self._transport = transport
        self._call_timeout_s = call_timeout_s
        self.consecutive_errors = 0
        self.protocol_version = ""
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self._on_tools_changed: Callable[[], Awaitable[None]] | None = None
        transport.on_notification = self._handle_notification

    @property
    def alive(self) -> bool:
        return self._transport.alive

    @property
    def stderr_tail(self) -> list[str]:
        return getattr(self._transport, "stderr_tail", [])

    def set_tools_changed_handler(self, handler: Callable[[], Awaitable[None]]) -> None:
        self._on_tools_changed = handler

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == N_TOOLS_CHANGED and self._on_tools_changed is not None:
            await self._on_tools_changed()

    async def _request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> Any:
        try:
            result = await self._transport.send_request(
                method, params or {}, timeout_s=timeout_s or self._call_timeout_s
            )
        except RpcError as exc:
            if exc.code in _FAULT_CODES:
                self.consecutive_errors += 1
            raise
        self.consecutive_errors = 0
        return result

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self._transport.start()

    async def initialize(self, *, timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S) -> None:
        """The MCP handshake: ``initialize`` then the ``notifications/initialized`` ack.
        Refuses a server whose protocol version we do not support (R3)."""
        result = await self._request(
            M_INITIALIZE,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": CLIENT_INFO,
            },
            timeout_s=timeout_s,
        )
        info = result if isinstance(result, dict) else {}
        version = str(info.get("protocolVersion", ""))
        # A missing/empty version is a spec violation; a present-but-unknown one is
        # unsupported. Both are refused BEFORE the initialized ack (R3).
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise RpcError(
                jsonrpc.INVALID_REQUEST,
                f"MCP server speaks unsupported protocol {version or '(none)'!r} "
                f"(TITAN supports {sorted(SUPPORTED_PROTOCOL_VERSIONS)})",
            )
        self.protocol_version = version
        self.server_info = dict(info.get("serverInfo") or {})
        self.capabilities = dict(info.get("capabilities") or {})
        # Subsequent requests must carry the NEGOTIATED version (HTTP header); no-op on stdio.
        self._transport.set_protocol_version(version)
        await self._transport.send_notification(N_INITIALIZED, {})

    async def health(self, *, timeout_s: float = 10.0) -> bool:
        try:
            await self._request(M_PING, timeout_s=timeout_s)
        except RpcError:
            return False
        return True

    async def stop(self, *, drain_timeout_s: float = 0.0) -> None:
        # An MCP server has no in-flight capability callbacks to drain; the pending
        # requests are failed by the transport on stop.
        _ = drain_timeout_s
        await self._transport.stop()

    # --- discovery ----------------------------------------------------------

    async def list_tools(self) -> list[McpTool]:
        tools: list[McpTool] = []
        for page in await self._paginate(M_TOOLS_LIST, "tools"):
            if not isinstance(page, dict):
                continue
            tools.append(
                McpTool(
                    name=str(page.get("name", "")),
                    description=str(page.get("description", "")),
                    input_schema=dict(page.get("inputSchema") or {}),
                )
            )
        return [t for t in tools if t.name]

    async def list_resources(self) -> list[McpResource]:
        if "resources" not in self.capabilities:
            return []
        resources: list[McpResource] = []
        for item in await self._paginate(M_RESOURCES_LIST, "resources"):
            if not isinstance(item, dict):
                continue
            resources.append(
                McpResource(
                    uri=str(item.get("uri", "")),
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    mime_type=str(item.get("mimeType", "")),
                )
            )
        return [r for r in resources if r.uri]

    async def list_prompts(self) -> list[McpPrompt]:
        if "prompts" not in self.capabilities:
            return []
        prompts: list[McpPrompt] = []
        for item in await self._paginate(M_PROMPTS_LIST, "prompts"):
            if not isinstance(item, dict):
                continue
            prompts.append(
                McpPrompt(
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    arguments=list(item.get("arguments") or []),
                )
            )
        return [p for p in prompts if p.name]

    async def _paginate(self, method: str, key: str) -> list[Any]:
        """Follow ``nextCursor`` to the end. Bounded so a misbehaving server that never
        stops paging cannot loop forever."""
        items: list[Any] = []
        cursor: str | None = None
        for _ in range(100):
            params = {"cursor": cursor} if cursor else {}
            try:
                result = await self._request(method, params)
            except RpcError as exc:
                if exc.code == jsonrpc.METHOD_NOT_FOUND:
                    return items  # server does not implement this capability
                raise
            if not isinstance(result, dict):
                break
            items.extend(result.get(key) or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return items

    # --- invocation ---------------------------------------------------------

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request(M_TOOLS_CALL, {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            return {"result": result}
        content = result.get("content") or []
        out: dict[str, Any] = {"content": content, "isError": bool(result.get("isError", False))}
        text = "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text:
            out["text"] = text
        if "structuredContent" in result:
            out["structuredContent"] = result["structuredContent"]
        return out

    async def read_resource(self, uri: str) -> dict[str, Any]:
        result = await self._request(M_RESOURCES_READ, {"uri": uri})
        return {"contents": (result or {}).get("contents", [])} if isinstance(result, dict) else {}

    async def get_prompt(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request(M_PROMPTS_GET, {"name": name, "arguments": arguments or {}})
        return dict(result) if isinstance(result, dict) else {}


__all__ = [
    "CLIENT_INFO",
    "DEFAULT_CALL_TIMEOUT_S",
    "MCP_PROTOCOL_VERSION",
    "N_TOOLS_CHANGED",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "HttpMcpTransport",
    "McpClient",
    "McpPrompt",
    "McpResource",
    "McpTool",
    "McpTransport",
    "StdioMcpTransport",
]
