"""The out-of-process plugin worker (Vol 7 §3.5, ADR-INT-01, TITAN-INT-016).

Core NEVER imports plugin code. Each plugin runs as its own subprocess
(``python -I`` — isolated mode: no site-packages injection, no ``PYTHONPATH`` leakage
from the parent) speaking newline-delimited JSON-RPC 2.0 on stdio, so:

* a plugin that crashes (``os._exit(1)``) cannot take core down — its pending calls
  resolve as a structured ``PLUGIN_CRASHED`` error;
* a plugin that hangs cannot block core — every call is bounded by a timeout
  (default 60 s, per-tool overridable) and the reader runs on its own task.

The channel is bidirectional: the plugin's capability requests (``titan/*``) arrive on
the same stream and are brokered by an injected callback, which is where the five
deny-by-default permission classes are enforced.
"""

import asyncio
import contextlib
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog

from titan.plugins import jsonrpc
from titan.plugins.jsonrpc import Request, Response, RpcError

logger = structlog.get_logger("titan.plugins.process")

#: The stdlib-only plugin-side host, launched by absolute path so the plugin's own
#: venv interpreter (which does not have titan installed) can run it.
HOST_SCRIPT = str(Path(__file__).with_name("host.py"))

#: Broker signature: (method, params) -> result. Raises RpcError to deny.
CapabilityBroker = Callable[[str, dict[str, Any]], Awaitable[Any]]

DEFAULT_CALL_TIMEOUT_S = 60.0
DEFAULT_HEALTH_TIMEOUT_S = 10.0
_STOP_GRACE_S = 2.0
_DRAIN_TIMEOUT_S = 5.0  # bound on a single stdin write to a non-reading child
_STREAM_LIMIT = 16 * 1024 * 1024  # 16 MiB max JSON-RPC frame (default is 64 KiB)

# Only these error codes indicate a plugin MALFUNCTION and count toward the 3-strike
# auto-quarantine (TITAN-INT-014: "three consecutive unhandled exceptions"). A
# PERMISSION_DENIED is the security model working, and INVALID_PARAMS/METHOD_NOT_FOUND
# are the caller's fault — none of those may quarantine a well-behaved plugin.
_FAULT_CODES = frozenset({jsonrpc.INTERNAL_ERROR, jsonrpc.TIMEOUT, jsonrpc.PLUGIN_CRASHED})


class PluginProcess:
    """One running plugin subprocess. Not reused across enable cycles."""

    def __init__(
        self,
        plugin_id: str,
        *,
        entrypoint: str,
        cwd: str,
        python_exe: str | None = None,
        broker: CapabilityBroker | None = None,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
        env: dict[str, str] | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self._entrypoint = entrypoint
        self._cwd = cwd
        self._python = python_exe or sys.executable
        self._broker = broker
        self._call_timeout_s = call_timeout_s
        self._env = env or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._draining: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Response]] = {}
        self._next_id = 0
        # Capability-serving tasks are spawned from the reader; hold strong references
        # (a bare create_task can be GC'd mid-flight, hanging the plugin waiting for a
        # reply that is never written).
        self._capability_tasks: set[asyncio.Task[None]] = set()
        #: Consecutive unhandled plugin errors — three quarantines it (TITAN-INT-014).
        self.consecutive_errors = 0
        #: The tail of the worker's stderr, for the operator's quarantine report.
        self.stderr_tail: list[str] = []

    # --- lifecycle ----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        # `python -I` (isolated) ignores PYTHONPATH and never adds the parent's site
        # dirs, so the host is launched by absolute SCRIPT PATH (it is stdlib-only and
        # therefore runnable by the plugin's own venv interpreter, which need not have
        # titan installed) and it puts the plugin dir on sys.path itself.
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1", **self._env}
        self._proc = await asyncio.create_subprocess_exec(
            self._python,
            "-I",
            HOST_SCRIPT,
            self._cwd,
            self._entrypoint,
            cwd=self._cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # A single JSON-RPC frame (e.g. a large tool result) can exceed the default
            # 64 KiB StreamReader limit; readline() would then raise and kill the reader.
            # Raise it so ordinary large-but-valid results do not break the transport.
            limit=_STREAM_LIMIT,
        )
        self._reader = asyncio.create_task(self._read_loop())
        # stderr MUST be drained continuously: an undrained pipe fills, the child
        # blocks on write, and it then cannot be killed on time (the same hazard the
        # sandbox controller documents). We keep only a bounded tail.
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
                text = line.decode("utf-8", errors="replace").rstrip()
                self.stderr_tail.append(text)
                del self.stderr_tail[:-50]  # bounded: keep the last 50 lines
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — draining must never kill core
            logger.debug("plugin.stderr_drain_stopped", plugin_id=self.plugin_id)

    async def stop(self, *, drain_timeout_s: float = 0.0) -> None:
        """Terminate the worker. With ``drain_timeout_s`` > 0 the currently in-flight
        calls are given that long to complete first (a graceful disable, §3.3: "in-flight
        calls drain"); any still pending after the window resolve as PLUGIN_CRASHED."""
        if drain_timeout_s > 0 and self._pending:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*self._pending.values(), return_exceptions=True),
                    timeout=drain_timeout_s,
                )
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
        for task in (self._reader, self._draining):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._fail_pending(RpcError(jsonrpc.PLUGIN_CRASHED, "plugin worker stopped"))
        self._proc = None
        self._reader = None
        self._draining = None

    # --- protocol -----------------------------------------------------------

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> Any:
        """One core→plugin request. Raises :class:`RpcError` on denial, timeout, or
        a dead worker — never lets a plugin fault escape as an arbitrary exception."""
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise RpcError(jsonrpc.PLUGIN_CRASHED, f"plugin {self.plugin_id} is not running")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._proc.stdin.write(
            jsonrpc.encode(Request(method=method, params=params or {}, id=request_id))
        )
        try:
            # Bound the SEND too: a plugin that stops reading its stdin fills the pipe
            # buffer and an unbounded drain() would block the writer indefinitely.
            await asyncio.wait_for(self._proc.stdin.drain(), timeout=_DRAIN_TIMEOUT_S)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(request_id, None)
            raise RpcError(jsonrpc.PLUGIN_CRASHED, f"plugin {self.plugin_id} died") from exc
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            self.consecutive_errors += 1
            raise RpcError(
                jsonrpc.TIMEOUT, f"plugin {self.plugin_id} stopped reading its input"
            ) from exc

        budget = timeout_s if timeout_s is not None else self._call_timeout_s
        try:
            response = await asyncio.wait_for(future, timeout=budget)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            self.consecutive_errors += 1
            raise RpcError(
                jsonrpc.TIMEOUT, f"plugin {self.plugin_id} call '{method}' exceeded {budget:g}s"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

        if response.error is not None:
            # Only a genuine plugin fault advances the quarantine streak; a legitimate
            # PERMISSION_DENIED or a bad-params error must not quarantine the plugin.
            if response.error.code in _FAULT_CODES:
                self.consecutive_errors += 1
            raise response.error
        self.consecutive_errors = 0
        return response.result

    async def health(self, *, timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S) -> bool:
        """The §3.3 enable healthcheck — must return OK within 10 s."""
        try:
            result = await self.call(jsonrpc.M_HEALTH, timeout_s=timeout_s)
        except RpcError:
            return False
        return bool((result or {}).get("ok", False))

    # --- the reader: responses + inbound capability requests ----------------

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break  # EOF — the worker exited
                try:
                    message = jsonrpc.decode(line)
                except RpcError:
                    continue  # a non-protocol line; ignore rather than die
                if "method" in message:
                    # A plugin→core CAPABILITY request — broker it (deny-by-default).
                    # Hold a strong ref so the task cannot be GC'd before it replies.
                    task = asyncio.create_task(self._serve_capability(message))
                    self._capability_tasks.add(task)
                    task.add_done_callback(self._capability_tasks.discard)
                else:
                    response = jsonrpc.parse_response(message)
                    future = (
                        self._pending.get(int(response.id)) if response.id is not None else None
                    )
                    if future is not None and not future.done():
                        future.set_result(response)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a reader fault must not kill core
            logger.exception("plugin.reader_error", plugin_id=self.plugin_id)
        finally:
            # The worker is gone: every in-flight call fails structurally, not by hanging.
            self._fail_pending(RpcError(jsonrpc.PLUGIN_CRASHED, f"plugin {self.plugin_id} exited"))

    async def _serve_capability(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            if self._broker is None:
                raise RpcError(jsonrpc.PERMISSION_DENIED, "no capability broker configured")
            result = await self._broker(method, dict(params))
            reply = Response(id=request_id, result=result)
        except RpcError as exc:
            reply = Response(id=request_id, error=exc)
        except Exception as exc:  # noqa: BLE001
            reply = Response(id=request_id, error=RpcError(jsonrpc.INTERNAL_ERROR, str(exc)))
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
            proc.stdin.write(jsonrpc.encode(reply))
            await proc.stdin.drain()

    def _fail_pending(self, error: RpcError) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result(Response(id=None, error=error))
        self._pending.clear()


__all__ = [
    "DEFAULT_CALL_TIMEOUT_S",
    "DEFAULT_HEALTH_TIMEOUT_S",
    "CapabilityBroker",
    "PluginProcess",
]
