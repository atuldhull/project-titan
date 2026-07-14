"""The plugin-side host — the program that runs INSIDE the per-plugin subprocess
(Vol 7 §3.5, ADR-INT-01). Launched as ``python -I -m titan.plugins.host <entrypoint>``.

STDLIB-ONLY on purpose: the plugin runs in its own isolated virtualenv which is NOT
the core environment, so this host may not assume pydantic/sqlalchemy are importable.
It speaks newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP framing).

It serves core→plugin methods (``initialize``, ``tools/list``, ``tools/call``,
``health``, ``webhook``) by delegating to the plugin object named by the entrypoint,
and it exposes a :class:`Capabilities` handle the plugin uses to ASK core for any
privileged capability (secrets, network, events, filesystem) — core brokers each one
deny-by-default, which is what makes the five permission classes enforceable.

stdout is reserved for the protocol: the plugin's own prints are redirected to stderr
so a stray print cannot corrupt the frame stream.
"""

import importlib
import json
import sys
import threading
from typing import Any

JSONRPC = "2.0"
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
INVALID_PARAMS = -32602
PERMISSION_DENIED = -31001  # must mirror titan.plugins.jsonrpc (kept literal: stdlib-only)

_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr  # a plugin's print() must never corrupt the JSON-RPC stream

_lock = threading.Lock()
_next_id = [1000]


def _write(message: dict[str, Any]) -> None:
    with _lock:
        _PROTOCOL_OUT.write(json.dumps(message, separators=(",", ":")) + "\n")
        _PROTOCOL_OUT.flush()


class CapabilityDenied(Exception):
    """Core refused a capability the plugin did not declare / was not granted. It
    carries core's ORIGINAL error code so the denial survives the round-trip: if the
    host collapsed it into a generic INTERNAL_ERROR, core would re-map it to
    UPSTREAM_ERROR and a PERMISSION_DENIED would be indistinguishable from a bug."""

    def __init__(self, message: str, code: int = PERMISSION_DENIED) -> None:
        super().__init__(message)
        self.code = code


class Capabilities:
    """The plugin's handle to core-brokered capabilities. Every call is a JSON-RPC
    request BACK to core, which checks the operator-granted manifest permissions and
    fails closed with PERMISSION_DENIED (§3.4, TITAN-INT-015)."""

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        with _lock:
            _next_id[0] += 1
            request_id = _next_id[0]
        _write({"jsonrpc": JSONRPC, "id": request_id, "method": method, "params": params})
        # Core replies on stdin, interleaved with its own requests; read until ours lands.
        while True:
            line = sys.stdin.readline()
            if not line:
                raise CapabilityDenied("core closed the channel")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    err = message["error"] or {}
                    raise CapabilityDenied(
                        str(err.get("message", "denied")),
                        code=int(err.get("code", PERMISSION_DENIED)),
                    )
                return message.get("result")
            # Not our reply — a core→plugin request arriving mid-capability. Serve it
            # after we return; queue it so ordering is preserved.
            _PENDING.append(message)

    def secret(self, name: str) -> str:
        return str(self._call("titan/secret.get", {"name": name}))

    def fetch(self, url: str, method: str = "GET", body: str | None = None) -> dict[str, Any]:
        result = self._call("titan/net.fetch", {"url": url, "method": method, "body": body})
        return dict(result or {})

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self._call("titan/event.publish", {"topic": topic, "payload": payload})

    def read_file(self, path: str) -> str:
        return str(self._call("titan/fs.read", {"path": path}))


_PENDING: list[dict[str, Any]] = []


def _load_plugin(entrypoint: str, capabilities: Capabilities) -> Any:
    """``module:Class`` → an instance. The plugin receives its capability handle."""
    module_name, _, attr = entrypoint.partition(":")
    if not module_name or not attr:
        raise ValueError(f"entrypoint must be 'module:Class', got {entrypoint!r}")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr)
    try:
        return factory(capabilities)
    except TypeError:
        return factory()


def _dispatch(plugin: Any, method: str, params: dict[str, Any]) -> Any:
    if method == "initialize":
        return {"protocol": JSONRPC, "plugin": getattr(plugin, "id", "unknown")}
    if method == "health":
        health = getattr(plugin, "health", None)
        return {"ok": bool(health()) if callable(health) else True}
    if method == "tools/list":
        lister = getattr(plugin, "tools", None)
        return {"tools": list(lister()) if callable(lister) else []}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            raise ValueError("params.name is required")
        caller = getattr(plugin, "call_tool", None)
        if not callable(caller):
            raise ValueError("plugin does not implement call_tool")
        return {"result": caller(name, arguments)}
    if method == "webhook":
        handler = getattr(plugin, "on_webhook", None)
        if not callable(handler):
            return {"handled": False}
        return {"handled": True, "result": handler(params.get("path", ""), params.get("body"))}
    raise LookupError(method)


def serve(plugin_dir: str, entrypoint: str) -> None:
    # `python -I` is isolated mode (implies -E), so PYTHONPATH is ignored and the
    # script's own directory — not the plugin's — lands on sys.path. Put the plugin
    # dir on the path explicitly; its venv already supplies its dependencies.
    if plugin_dir and plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    capabilities = Capabilities()
    plugin: Any = None
    load_error: str | None = None
    try:
        plugin = _load_plugin(entrypoint, capabilities)
    except Exception as exc:  # noqa: BLE001 — report, don't die: core must see the error
        load_error = f"{type(exc).__name__}: {exc}"

    while True:
        line = _PENDING.pop(0) if _PENDING else None
        if line is None:
            raw = sys.stdin.readline()
            if not raw:
                return  # core closed the channel
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
        else:
            message = line
        if "method" not in message:
            continue  # a stray capability reply with no matching waiter
        request_id = message.get("id")
        method = str(message.get("method"))
        params = message.get("params") or {}
        if load_error is not None:
            _write(
                {
                    "jsonrpc": JSONRPC,
                    "id": request_id,
                    "error": {
                        "code": INTERNAL_ERROR,
                        "message": f"entrypoint failed: {load_error}",
                    },
                }
            )
            continue
        try:
            result = _dispatch(plugin, method, params)
        except LookupError:
            _write(
                {
                    "jsonrpc": JSONRPC,
                    "id": request_id,
                    "error": {"code": METHOD_NOT_FOUND, "message": f"unknown method {method}"},
                }
            )
            continue
        except CapabilityDenied as exc:
            # A denial must reach core with its ORIGINAL code (PERMISSION_DENIED), not be
            # collapsed into a generic internal error — otherwise a refused capability is
            # indistinguishable from a plugin bug and would not fail closed visibly.
            _write(
                {
                    "jsonrpc": JSONRPC,
                    "id": request_id,
                    "error": {"code": exc.code, "message": str(exc)},
                }
            )
            continue
        except ValueError as exc:
            _write(
                {
                    "jsonrpc": JSONRPC,
                    "id": request_id,
                    "error": {"code": INVALID_PARAMS, "message": str(exc)},
                }
            )
            continue
        except Exception as exc:  # noqa: BLE001 — an unhandled plugin error is reported,
            # not fatal; core counts three of these and quarantines (TITAN-INT-014).
            _write(
                {
                    "jsonrpc": JSONRPC,
                    "id": request_id,
                    "error": {
                        "code": INTERNAL_ERROR,
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }
            )
            continue
        if request_id is not None:
            _write({"jsonrpc": JSONRPC, "id": request_id, "result": result})


if __name__ == "__main__":  # pragma: no cover — exercised as a subprocess
    if len(sys.argv) < 3:
        sys.stderr.write("usage: python -I host.py <plugin_dir> <module:Class>\n")
        raise SystemExit(2)
    serve(sys.argv[1], sys.argv[2])
