"""A deterministic, spec-compliant MCP server over stdio (Vol 7 §7, R3 conformance).

STDLIB ONLY — it is spawned as a subprocess by ``command`` (exactly as a real MCP server
such as @modelcontextprotocol/server-filesystem would be), so it cannot import anything a
fresh interpreter lacks. It implements the handshake, tools/resources/prompts discovery,
``tools/call``, ``ping``, and — via the ``grow`` tool — a live ``tools/list_changed``
notification so the client's re-registration path can be proven end to end.

The message handler :func:`handle` is importable so the Streamable-HTTP reference app can
reuse the exact same behaviour over a different transport.
"""

import json
import sys
from collections.abc import Callable
from typing import Any

_Ok = Callable[[dict[str, Any]], dict[str, Any]]
_Err = Callable[[int, str], dict[str, Any]]

PROTOCOL_VERSION = "2025-06-18"

_BASE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "Echo the arguments back as JSON.",
        "inputSchema": {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "additionalProperties": True,
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    },
    {
        "name": "boom",
        "description": "Always faults with a JSON-RPC error (fault-isolation probe).",
        "inputSchema": {"type": "object"},
    },
    {
        "name": "tool_error",
        "description": "Returns a tool-level error result (isError=true), not a fault.",
        "inputSchema": {"type": "object"},
    },
    {
        "name": "grow",
        "description": "Advertise a new tool and emit notifications/tools/list_changed.",
        "inputSchema": {"type": "object"},
    },
    {
        "name": "bad_schema",
        "description": "Advertises a STRUCTURALLY-INVALID inputSchema (a typo'd nested type).",
        # 'strig' is not a valid JSON Schema type: check_schema raises SchemaError, which the
        # client must downgrade to a permissive schema rather than failing the whole server.
        "inputSchema": {"type": "object", "properties": {"x": {"type": "strig"}}},
    },
]

_LATE_TOOL: dict[str, Any] = {
    "name": "late_tool",
    "description": "Only advertised after 'grow' fires list_changed.",
    "inputSchema": {"type": "object"},
}

_RESOURCES: list[dict[str, Any]] = [
    {
        "uri": "mem://note",
        "name": "note",
        "description": "an in-memory note",
        "mimeType": "text/plain",
    }
]
_PROMPTS: list[dict[str, Any]] = [
    {
        "name": "greet",
        "description": "A greeting prompt.",
        "arguments": [{"name": "who", "description": "who to greet", "required": True}],
    }
]


def make_state() -> dict[str, Any]:
    """Fresh per-connection server state. ``notifications`` queues server→client frames
    to flush before the triggering response."""
    return {"grown": False, "notifications": []}


def handle(message: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    """One JSON-RPC message → its response dict, or None for a notification (no reply).
    Side effects (the list_changed emission) are queued on ``state['notifications']``."""
    method = message.get("method")
    mid = message.get("id")
    params = message.get("params") or {}
    if method is None or mid is None:
        return None  # a response echo, or a client notification (initialized) — ignore

    def ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code: int, msg: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {},
                    "prompts": {},
                },
                "serverInfo": {"name": "titan-reference-mcp", "version": "1.0.0"},
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        tools = [*_BASE_TOOLS, *([_LATE_TOOL] if state["grown"] else [])]
        return ok({"tools": tools})
    if method == "tools/call":
        return _call_tool(
            str(params.get("name", "")), params.get("arguments") or {}, state, ok, err
        )
    if method == "resources/list":
        return ok({"resources": _RESOURCES})
    if method == "resources/read":
        uri = str(params.get("uri", ""))
        if uri == "mem://note":
            return ok({"contents": [{"uri": uri, "mimeType": "text/plain", "text": "note body"}]})
        return err(-32602, f"unknown resource: {uri}")
    if method == "prompts/list":
        return ok({"prompts": _PROMPTS})
    if method == "prompts/get":
        who = str((params.get("arguments") or {}).get("who", ""))
        return ok(
            {
                "description": "A greeting prompt.",
                "messages": [{"role": "user", "content": {"type": "text", "text": f"hi {who}"}}],
            }
        )
    return err(-32601, f"method not found: {method}")


def _call_tool(
    name: str, args: dict[str, Any], state: dict[str, Any], ok: _Ok, err: _Err
) -> dict[str, Any]:
    if name == "echo":
        return ok({"content": [{"type": "text", "text": json.dumps(args)}]})
    if name == "add":
        total = float(args.get("a", 0)) + float(args.get("b", 0))
        return ok(
            {"content": [{"type": "text", "text": str(total)}], "structuredContent": {"sum": total}}
        )
    if name == "boom":
        return err(-32603, "boom: this tool always faults")
    if name == "tool_error":
        return ok({"content": [{"type": "text", "text": "the tool failed"}], "isError": True})
    if name == "grow":
        state["grown"] = True
        state["notifications"].append(
            {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
        )
        return ok({"content": [{"type": "text", "text": "grew a tool"}]})
    if name == "late_tool":
        return ok({"content": [{"type": "text", "text": "late tool result"}]})
    return err(-32602, f"unknown tool: {name}")


def main() -> None:
    state = make_state()
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message, state)
        for note in state["notifications"]:  # flush server-initiated frames first
            out.write((json.dumps(note) + "\n").encode("utf-8"))
        state["notifications"] = []
        if response is not None:
            out.write((json.dumps(response) + "\n").encode("utf-8"))
        out.flush()


if __name__ == "__main__":
    main()
