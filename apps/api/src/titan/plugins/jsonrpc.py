"""JSON-RPC 2.0 framing over stdio — deliberately identical in shape to MCP
(Vol 7 §3.5/§7, ADR-INT-01/02) so the plugin runtime and the Phase-53 MCP client
share ONE transport codebase.

Framing is newline-delimited JSON (MCP's stdio transport): one JSON-RPC message per
line. PURE + stdlib-only (no pydantic, no I/O) so the plugin-side host can import the
same shapes inside its isolated venv.

The channel is BIDIRECTIONAL:

* core → plugin: ``initialize``, ``tools/list``, ``tools/call``, ``health``, ``webhook``
* plugin → core: ``titan/*`` CAPABILITY requests (secret.get, net.fetch, event.publish,
  fs.read) — brokered and deny-by-default checked by core (§3.4). This is what makes
  the five permission classes actually enforceable: a subprocess cannot be trusted to
  police its own egress, so it must ASK core for every capability.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

JSONRPC_VERSION = "2.0"

# Standard JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# TITAN application codes (outside the reserved -32768..-32000 range).
PERMISSION_DENIED = -31001
TIMEOUT = -31002
PLUGIN_CRASHED = -31003

# --- core → plugin methods ---------------------------------------------------
M_INITIALIZE = "initialize"
M_TOOLS_LIST = "tools/list"
M_TOOLS_CALL = "tools/call"
M_HEALTH = "health"
M_WEBHOOK = "webhook"

# --- plugin → core capability methods (the brokered five classes) ------------
C_SECRET_GET = "titan/secret.get"
C_NET_FETCH = "titan/net.fetch"
C_EVENT_PUBLISH = "titan/event.publish"
C_FS_READ = "titan/fs.read"
C_SCOPE_CHECK = "titan/scope.check"

CAPABILITY_METHODS = frozenset(
    {C_SECRET_GET, C_NET_FETCH, C_EVENT_PUBLISH, C_FS_READ, C_SCOPE_CHECK}
)


@dataclass(frozen=True)
class RpcError(Exception):
    """A JSON-RPC error object, raised across the transport boundary."""

    code: int
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


@dataclass(frozen=True)
class Request:
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: int | str | None = None  # None ⇒ a notification (no response expected)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": self.method}
        if self.params:
            out["params"] = self.params
        if self.id is not None:
            out["id"] = self.id
        return out


@dataclass(frozen=True)
class Response:
    id: int | str | None
    result: Any = None
    error: RpcError | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": self.id}
        if self.error is not None:
            out["error"] = self.error.to_dict()
        else:
            out["result"] = self.result
        return out


def encode(message: Request | Response) -> bytes:
    """One JSON-RPC message as a single newline-terminated line (MCP stdio framing)."""
    return (json.dumps(message.to_dict(), separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict[str, Any]:
    """Decode one framed line into a raw JSON-RPC message dict."""
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RpcError(PARSE_ERROR, f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("jsonrpc") != JSONRPC_VERSION:
        raise RpcError(INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    return parsed


def parse_response(raw: dict[str, Any]) -> Response:
    """A raw message → a :class:`Response`, surfacing an error object as RpcError."""
    if "error" in raw:
        err = raw["error"] or {}
        return Response(
            id=raw.get("id"),
            error=RpcError(
                code=int(err.get("code", INTERNAL_ERROR)),
                message=str(err.get("message", "unknown error")),
                data=err.get("data"),
            ),
        )
    return Response(id=raw.get("id"), result=raw.get("result"))


def parse_request(raw: dict[str, Any]) -> Request:
    method = raw.get("method")
    if not isinstance(method, str):
        raise RpcError(INVALID_REQUEST, "missing 'method'")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise RpcError(INVALID_PARAMS, "'params' must be an object")
    return Request(method=method, params=params, id=raw.get("id"))


class Transport(Protocol):
    """The transport seam the plugin runtime and the MCP client share (ADR-INT-02).
    stdio is the Phase-52 implementation; Streamable HTTP lands with the MCP client."""

    async def send(self, message: Request | Response) -> None: ...

    async def receive(self) -> dict[str, Any] | None: ...

    async def close(self) -> None: ...


__all__ = [
    "CAPABILITY_METHODS",
    "C_EVENT_PUBLISH",
    "C_FS_READ",
    "C_NET_FETCH",
    "C_SCOPE_CHECK",
    "C_SECRET_GET",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "JSONRPC_VERSION",
    "METHOD_NOT_FOUND",
    "M_HEALTH",
    "M_INITIALIZE",
    "M_TOOLS_CALL",
    "M_TOOLS_LIST",
    "M_WEBHOOK",
    "PARSE_ERROR",
    "PERMISSION_DENIED",
    "PLUGIN_CRASHED",
    "TIMEOUT",
    "Request",
    "Response",
    "RpcError",
    "Transport",
    "decode",
    "encode",
    "parse_request",
    "parse_response",
]
