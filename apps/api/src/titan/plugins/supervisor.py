"""The plugin supervisor — owns the running out-of-process workers, contributes their
tools to the EXISTING Tool Registry, and auto-quarantines misbehaving plugins
(Vol 7 §3.3/§3.5, TITAN-INT-014/016).

Plugin tools are ordinary registry entries with ``provider='plugin:<id>'``. They are
dispatched by the Phase-19 Tool Bus like any other tool — schema validation, role
allowlist, scope check, approval gate for admin risk, and the hash-chained audit all
apply unchanged. There is deliberately NO parallel dispatch path: the executor we
register is a thin async proxy that forwards the call to the plugin's subprocess.
"""

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.events.service import STREAM_EVENTS_DOMAIN, write_event
from titan.messaging import service as messaging
from titan.plugins import jsonrpc
from titan.plugins import repository as repo
from titan.plugins.capabilities import CapabilityBroker, HttpFetcher
from titan.plugins.jsonrpc import RpcError
from titan.plugins.manifest import Permissions, PluginManifest
from titan.plugins.process import PluginProcess
from titan.plugins.secrets import SecretsProvider
from titan.tools.service import (
    ExecutionOutcome,
    ToolError,
    ToolExecutionContext,
    ToolRegistry,
    ToolSpec,
)

logger = structlog.get_logger("titan.plugins.supervisor")

#: Three consecutive unhandled plugin errors quarantine it (TITAN-INT-014).
MAX_CONSECUTIVE_ERRORS = 3
QUARANTINE_EVENT = "plugin.quarantined"

_PERMISSIVE_INPUT: dict[str, Any] = {"type": "object", "additionalProperties": True}
_RESULT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}


def _tool_error_for(exc: RpcError) -> ToolError:
    """A plugin fault becomes a STRUCTURED tool error — never an escaping exception
    (TITAN-INT-016: a crashing or hanging plugin must not take core down)."""
    if exc.code == jsonrpc.PERMISSION_DENIED:
        return ToolError("PERMISSION_DENIED", exc.message)
    if exc.code == jsonrpc.TIMEOUT:
        return ToolError("TIMEOUT", exc.message)
    if exc.code == jsonrpc.PLUGIN_CRASHED:
        return ToolError("TOOL_UNAVAILABLE", exc.message)
    return ToolError("UPSTREAM_ERROR", exc.message)


class PluginSupervisor:
    """One per process. Tolerates being constructed twice (the test suite builds a
    second app on the same DB) — it never spawns anything until :meth:`start_plugin`."""

    def __init__(
        self,
        engine: AsyncEngine,
        registry: ToolRegistry,
        *,
        secrets: SecretsProvider,
        http_fetcher: HttpFetcher | None = None,
        call_timeout_s: float = 60.0,
        workspace_root: Path | None = None,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._secrets = secrets
        self._http_fetcher = http_fetcher
        self._call_timeout_s = call_timeout_s
        self._workspace_root = workspace_root
        self._processes: dict[str, PluginProcess] = {}
        self._brokers: dict[str, CapabilityBroker] = {}
        self._tools: dict[str, list[str]] = {}
        # Auto-quarantine runs off the caller's error path, but a bare
        # asyncio.create_task() may be garbage-collected before it completes — hold a
        # strong reference until it finishes.
        self._quarantines: set[asyncio.Task[None]] = set()

    # --- introspection ------------------------------------------------------

    def running(self) -> list[str]:
        return sorted(self._processes)

    def tools_of(self, plugin_id: str) -> list[str]:
        return list(self._tools.get(plugin_id, []))

    def broker_of(self, plugin_id: str) -> CapabilityBroker | None:
        return self._brokers.get(plugin_id)

    # --- lifecycle ----------------------------------------------------------

    async def start_plugin(
        self,
        manifest: PluginManifest,
        granted: Permissions,
        *,
        plugin_dir: str,
        python_exe: str | None = None,
        health_timeout_s: float = 10.0,
    ) -> None:
        """Spawn the worker, healthcheck it (§3.3: OK within 10 s), then contribute its
        tools. A failed healthcheck leaves NOTHING registered — enable is all-or-nothing."""
        plugin_id = manifest.id
        await self.stop_plugin(plugin_id)  # idempotent re-enable

        broker = CapabilityBroker(
            plugin_id,
            granted,
            workspace_root=self._workspace_root,
            secret_resolver=self._secrets.get,
            event_publisher=self._publish_event,
            http_fetcher=self._http_fetcher,
            auditor=self._audit_denial,
        )
        process = PluginProcess(
            plugin_id,
            entrypoint=manifest.runtime.entrypoint or "",
            cwd=plugin_dir,
            python_exe=python_exe,
            broker=broker,
            call_timeout_s=self._call_timeout_s,
        )
        await process.start()
        if not await process.health(timeout_s=health_timeout_s):
            tail = "; ".join(process.stderr_tail[-5:])
            await process.stop()
            raise RuntimeError(
                f"plugin '{plugin_id}' failed its healthcheck: {tail or 'no output'}"
            )

        self._processes[plugin_id] = process
        self._brokers[plugin_id] = broker
        try:
            self._register_tools(manifest, granted)
        except Exception:
            # Registration is the last step; if a bad tool spec raises, tear the whole
            # thing back down so a failed start leaves NOTHING behind (the process, the
            # broker, and any partially-registered tools are all removed) — enable is
            # genuinely all-or-nothing (TITAN-INT-013).
            await self.stop_plugin(plugin_id)
            raise

    async def stop_plugin(self, plugin_id: str, *, drain_timeout_s: float = 0.0) -> list[str]:
        """Unload the plugin's tools and terminate its worker. Returns the tool names
        withdrawn. Unregistering FIRST rejects NEW calls (TOOL_UNAVAILABLE); a positive
        ``drain_timeout_s`` then lets in-flight calls finish before terminating (§3.3)."""
        removed = self._unregister_tools(plugin_id)
        process = self._processes.pop(plugin_id, None)
        self._brokers.pop(plugin_id, None)
        if process is not None:
            await process.stop(drain_timeout_s=drain_timeout_s)
        return removed

    async def quarantine(self, plugin_id: str, reason: str) -> None:
        """Unload + stop + persist QUARANTINED + emit ``plugin.quarantined`` + notify an
        operator, all in one transaction (TITAN-INT-014)."""
        removed = await self.stop_plugin(plugin_id)
        async with self._engine.begin() as conn:
            install = await repo.get_install(conn, plugin_id)
            if install is None:
                return
            await repo.set_state(
                conn,
                plugin_id,
                state="quarantined",
                last_error=reason[:2000],
                quarantined=True,
            )
            await write_event(
                conn,
                STREAM_EVENTS_DOMAIN,
                {
                    "type": QUARANTINE_EVENT,
                    "plugin": {
                        "plugin_id": plugin_id,
                        "reason": reason[:500],
                        "tools_unregistered": removed,
                    },
                },
                actor="plugin-supervisor",
            )
            owner = await repo.project_owner(conn)
            if owner is not None:
                await messaging.create_notification(
                    conn,
                    user_id=owner,
                    category="plugin",
                    severity="critical",
                    title=f"Plugin '{plugin_id}' quarantined",
                    body=f"{reason}\n\nTools unregistered: {', '.join(removed) or 'none'}",
                    link=f"/plugins/{plugin_id}",
                    actor="plugin-supervisor",
                )
        logger.warning("plugin.quarantined", plugin_id=plugin_id, reason=reason, tools=removed)

    async def sweep_health(self, *, health_timeout_s: float = 10.0) -> list[str]:
        """Healthcheck every running plugin; quarantine any that fails (TITAN-INT-014:
        "a failed healthcheck MUST quarantine"). Driven by a periodic monitor so the
        healthcheck half of the OR is real, not only checked once at enable. Returns the
        plugin ids quarantined this pass."""
        quarantined: list[str] = []
        for plugin_id in list(self._processes):
            process = self._processes.get(plugin_id)
            if process is None:
                continue
            if not await process.health(timeout_s=health_timeout_s):
                await self.quarantine(plugin_id, "healthcheck failed")
                quarantined.append(plugin_id)
        return quarantined

    async def drain_quarantines(self) -> None:
        """Await any in-flight auto-quarantine. Exposed so callers (and tests) can
        observe the terminal state deterministically instead of racing the loop."""
        while self._quarantines:
            pending = list(self._quarantines)
            await asyncio.gather(*pending, return_exceptions=True)

    async def aclose(self) -> None:
        await self.drain_quarantines()
        for plugin_id in list(self._processes):
            with contextlib.suppress(Exception):
                await self.stop_plugin(plugin_id)

    # --- tool registration ---------------------------------------------------

    def _register_tools(self, manifest: PluginManifest, granted: Permissions) -> None:
        names: list[str] = []
        for tool in manifest.contributes.tools:
            spec = ToolSpec(
                name=tool.name,
                provider=f"plugin:{manifest.id}",
                description=tool.description or f"{manifest.name} — {tool.name}",
                risk=tool.risk,
                # The plugin's OPERATOR-GRANTED scopes are what its tools require. These
                # never self-grant: runtime.grants excludes plugin providers from an
                # agent's authority, so the caller must already hold them (ADR-INT-04).
                required_scopes=tuple(granted.scopes),
                input_schema=tool.input_schema or dict(_PERMISSIVE_INPUT),
                output_schema=dict(_RESULT_SCHEMA),
                executor=self._make_executor(manifest.id, tool.name),
                timeout_s=tool.timeout_s or self._call_timeout_s,
            )
            self._registry.register(spec)
            names.append(tool.name)
        self._tools[manifest.id] = names

    def _unregister_tools(self, plugin_id: str) -> list[str]:
        # Removing the specs from the registry fully unloads the tools: the bus checks
        # registry.get() first (→ TOOL_UNAVAILABLE) and tools_for_turn iterates specs,
        # so no separate allowlist state is needed (that global would only leak).
        removed = self._registry.unregister_provider(f"plugin:{plugin_id}")
        self._tools.pop(plugin_id, None)
        return removed

    def _make_executor(self, plugin_id: str, tool_name: str) -> Any:
        async def _execute(
            arguments: dict[str, Any], context: ToolExecutionContext
        ) -> ExecutionOutcome:
            result = await self.call_tool(plugin_id, tool_name, arguments)
            return ExecutionOutcome(result=result)

        return _execute

    # --- dispatch ------------------------------------------------------------

    async def call_tool(
        self, plugin_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Proxy one call into the worker. Faults become structured ToolErrors, and
        three consecutive unhandled errors auto-quarantine the plugin."""
        process = self._processes.get(plugin_id)
        if process is None:
            raise ToolError("TOOL_UNAVAILABLE", f"plugin '{plugin_id}' is not enabled")
        try:
            payload = await process.call(
                jsonrpc.M_TOOLS_CALL, {"name": tool_name, "arguments": arguments}
            )
        except RpcError as exc:
            if process.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                # Off the caller's error path — quarantine must not block the response —
                # but tracked, so it cannot be GC'd and so aclose() can drain it.
                task = asyncio.create_task(
                    self.quarantine(
                        plugin_id,
                        f"{MAX_CONSECUTIVE_ERRORS} consecutive failures; last: {exc.message}",
                    )
                )
                self._quarantines.add(task)
                task.add_done_callback(self._quarantines.discard)
            raise _tool_error_for(exc) from exc
        result = (payload or {}).get("result")
        return result if isinstance(result, dict) else {"result": result}

    async def deliver_webhook(
        self, plugin_id: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        process = self._processes.get(plugin_id)
        if process is None:
            raise ToolError("TOOL_UNAVAILABLE", f"plugin '{plugin_id}' is not enabled")
        try:
            payload = await process.call(jsonrpc.M_WEBHOOK, {"path": path, "body": body})
        except RpcError as exc:
            raise _tool_error_for(exc) from exc
        return dict(payload or {})

    # --- capability side effects --------------------------------------------

    async def _publish_event(self, topic: str, payload: dict[str, Any]) -> None:
        async with self._engine.begin() as conn:
            await write_event(
                conn, STREAM_EVENTS_DOMAIN, {"type": topic, "data": payload}, actor="plugin"
            )

    async def _audit_denial(self, plugin_id: str, capability: str, detail: str) -> None:
        """A PERMISSION_DENIED capability touch is an auditable security event."""
        async with self._engine.begin() as conn:
            await write_event(
                conn,
                STREAM_EVENTS_DOMAIN,
                {
                    "type": "plugin.permission_denied",
                    "plugin": {
                        "plugin_id": plugin_id,
                        "capability": capability,
                        "detail": detail[:500],
                    },
                },
                actor="plugin-supervisor",
            )


def build_http_fetcher(client: Any) -> HttpFetcher:
    """A brokered HTTP fetcher over the shared httpx client. Redirects are DISABLED so a
    whitelisted host cannot 3xx-redirect a plugin to an internal/metadata address (SSRF);
    the allowlist is enforced by the broker BEFORE this runs. In production the egress
    sidecar proxy (§3.5) is the real network boundary; this is the local-dev path."""

    async def _fetch(url: str, method: str, body: str | None) -> dict[str, Any]:
        response = await client.request(
            method, url, content=body, follow_redirects=False, timeout=30.0
        )
        return {
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": response.text[:1_000_000],
        }

    return _fetch


async def run_health_monitor(
    supervisor: "PluginSupervisor", *, stop: Any, interval_s: float = 15.0
) -> None:  # pragma: no cover — live-verified loop
    """Periodically healthcheck enabled plugins and quarantine any that fail — the
    healthcheck half of TITAN-INT-014 ("within 30s"); a 15s cadence bounds detection."""
    while not stop.is_set():
        try:
            await supervisor.sweep_health()
        except Exception:  # noqa: BLE001
            logger.exception("plugin.health_monitor_error")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)


def venv_python(venv_path: str) -> str | None:
    """The interpreter inside a per-plugin virtualenv (never the core environment)."""
    if not venv_path:
        return None
    root = Path(venv_path)
    for candidate in (root / "bin" / "python", root / "Scripts" / "python.exe"):
        if candidate.exists():
            return str(candidate)
    return None


__all__ = [
    "MAX_CONSECUTIVE_ERRORS",
    "QUARANTINE_EVENT",
    "PluginSupervisor",
    "build_http_fetcher",
    "run_health_monitor",
    "venv_python",
]
