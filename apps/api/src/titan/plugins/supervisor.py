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
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import structlog
from jsonschema.exceptions import SchemaError
from jsonschema.validators import Draft202012Validator
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.events.service import STREAM_EVENTS_DOMAIN, write_event
from titan.messaging import service as messaging
from titan.plugins import jsonrpc
from titan.plugins import repository as repo
from titan.plugins.capabilities import CapabilityBroker, HttpFetcher, network_allowed
from titan.plugins.jsonrpc import RpcError
from titan.plugins.manifest import Permissions, PluginManifest
from titan.plugins.mcp import (
    HttpMcpTransport,
    McpClient,
    McpPrompt,
    McpResource,
    McpTool,
    McpTransport,
    StdioMcpTransport,
)
from titan.plugins.process import PluginProcess
from titan.plugins.secrets import ConfigCipher, SecretsProvider
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

#: Per ADR-INT-02, an MCP tool registers as ``write`` unless the operator downgrades it
#: per tool in configuration — external schemas are never trusted to self-declare risk.
MCP_DEFAULT_RISK = "write"
_VALID_RISK = frozenset({"read", "write", "admin"})
#: Overall wall-clock ceiling on the MCP handshake+discovery, so a server that slow-drips
#: paginated lists cannot wedge enable/re-registration for pages×per-call-timeout.
_DISCOVERY_DEADLINE_S = 120.0

_PERMISSIVE_INPUT: dict[str, Any] = {"type": "object", "additionalProperties": True}
_RESULT_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": True}
_RESOURCE_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {"uri": {"type": "string", "description": "The MCP resource URI to read."}},
    "required": ["uri"],
    "additionalProperties": False,
}
#: Any worker the supervisor owns — a first-party python plugin or an MCP client. Both
#: expose the small surface the shared lifecycle code touches (health/stop/errors).
Worker = PluginProcess | McpClient


def _sanitize_tool_segment(name: str) -> str:
    """An MCP server's tool name → one registry-legal name segment (``[a-z][a-z0-9_-]*``).
    The original name is kept in the context map, so ``tools/call`` still uses it."""
    slug = re.sub(r"[^a-z0-9_-]", "_", name.lower())
    if not slug or not slug[0].isalpha():
        slug = f"t_{slug}"
    return slug


@dataclass
class _McpContext:
    """Everything needed to re-register an MCP server's tools on a live ``list_changed``,
    plus what the introspection endpoint surfaces (resources/prompts)."""

    granted: Permissions
    risk_overrides: dict[str, str]
    call_timeout_s: float
    has_resources: bool
    protocol_version: str = ""
    server_info: dict[str, Any] = field(default_factory=dict)
    tool_names: dict[str, str] = field(default_factory=dict)  # display name -> mcp tool name
    resources: list[McpResource] = field(default_factory=list)
    prompts: list[McpPrompt] = field(default_factory=list)


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
        http_client: httpx.AsyncClient | None = None,
        config_cipher: ConfigCipher | None = None,
        call_timeout_s: float = 60.0,
        workspace_root: Path | None = None,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._secrets = secrets
        self._http_fetcher = http_fetcher
        # The shared httpx client the MCP Streamable-HTTP transport POSTs over (None →
        # only stdio MCP servers can be enabled).
        self._http_client = http_client
        # Decrypts the per-plugin config so an MCP server's per-tool risk overrides
        # (ADR-INT-02) can be read at enable time (None → every MCP tool stays write-risk).
        self._config_cipher = config_cipher
        self._call_timeout_s = call_timeout_s
        self._workspace_root = workspace_root
        self._processes: dict[str, Worker] = {}
        self._brokers: dict[str, CapabilityBroker] = {}
        self._tools: dict[str, list[str]] = {}
        # Per-MCP-server state, needed to re-register tools on a live list_changed and to
        # surface resources/prompts (a first-party python plugin has no entry here).
        self._mcp_ctx: dict[str, _McpContext] = {}
        # Auto-quarantine runs off the caller's error path, but a bare
        # asyncio.create_task() may be garbage-collected before it completes — hold a
        # strong reference until it finishes.
        self._quarantines: set[asyncio.Task[None]] = set()
        # Live tool re-registration (list_changed) also runs off a notification task —
        # tracked so it cannot be GC'd and so tests can observe it deterministically.
        self._mcp_tasks: set[asyncio.Task[None]] = set()
        # Coalesce re-registration: a hostile/chatty server that streams list_changed must
        # collapse to at most one in-flight + one queued pass per plugin (not a storm of
        # 100-round-trip re-discoveries). ``_active`` = a pass is running; ``_pending`` = a
        # notification arrived during it, so run exactly once more afterwards.
        self._reregister_active: set[str] = set()
        self._reregister_pending: set[str] = set()

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
        """Start a worker and contribute its tools. A first-party python plugin spawns its
        subprocess and is healthchecked (§3.3: OK within 10 s); an MCP server (§7) connects,
        performs ``initialize``, then discovers its tools. Either way a failed start leaves
        NOTHING registered — enable is all-or-nothing (TITAN-INT-013)."""
        if manifest.runtime.type == "mcp":
            await self._start_mcp(
                manifest, granted, plugin_dir=plugin_dir, handshake_timeout_s=health_timeout_s
            )
            return

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

    # --- MCP servers (Vol 7 §7, TITAN-INT-050) ------------------------------

    async def _start_mcp(
        self,
        manifest: PluginManifest,
        granted: Permissions,
        *,
        plugin_dir: str,
        handshake_timeout_s: float,
    ) -> None:
        """Connect to the MCP server, ``initialize``, discover its tools/resources/prompts,
        and register each tool as ``mcp.<id>.<tool>`` in the SAME registry the Tool Bus
        dispatches — no parallel path (ADR-INT-02)."""
        plugin_id = manifest.id
        await self.stop_plugin(plugin_id)  # idempotent re-enable
        transport = await self._build_mcp_transport(manifest, granted, plugin_dir=plugin_dir)
        client = McpClient(plugin_id, transport, call_timeout_s=self._call_timeout_s)
        await client.start()
        try:
            # One wall-clock bound over the WHOLE handshake+discovery: a server that
            # slow-drips paginated tool lists just under the per-call timeout must not be
            # able to wedge the operator's enable for ~pages×timeout (Vol 7 §7 R3).
            async with asyncio.timeout(_DISCOVERY_DEADLINE_S):
                await client.initialize(timeout_s=handshake_timeout_s)
                tools = await client.list_tools()
                resources = await client.list_resources()
                prompts = await client.list_prompts()
        except Exception as exc:  # noqa: BLE001 — any handshake failure tears the client down
            tail = "; ".join(client.stderr_tail[-5:])
            await client.stop()
            suffix = f" ({tail})" if tail else ""
            raise RuntimeError(
                f"MCP server '{plugin_id}' failed to initialize: {exc}{suffix}"
            ) from exc

        overrides = await self._load_risk_overrides(plugin_id)
        ctx = _McpContext(
            granted=granted,
            risk_overrides=overrides,
            call_timeout_s=self._call_timeout_s,
            has_resources="resources" in client.capabilities,
            protocol_version=client.protocol_version,
            server_info=client.server_info,
            resources=resources,
            prompts=prompts,
        )
        self._processes[plugin_id] = client
        self._mcp_ctx[plugin_id] = ctx
        try:
            self._tools[plugin_id] = self._register_mcp_tools(plugin_id, tools)
        except Exception:
            await self.stop_plugin(plugin_id)
            raise
        # Live re-registration on notifications/tools/list_changed. Scheduled as a tracked
        # task (never awaited on the transport's read path) so the round-trip cannot deadlock.
        client.set_tools_changed_handler(lambda: self._schedule_reregister(plugin_id))

    async def _load_risk_overrides(self, plugin_id: str) -> dict[str, str]:
        """Operator per-tool risk downgrades from the encrypted config (ADR-INT-02). Keyed
        by the server's ORIGINAL tool name; absent/undecryptable config → no overrides."""
        if self._config_cipher is None:
            return {}
        async with self._engine.connect() as conn:
            row = await repo.get_install(conn, plugin_id)
        if row is None:
            return {}
        try:
            config = self._config_cipher.decrypt(row.config_encrypted)
        except (ValueError, KeyError):
            return {}
        raw = config.get("risk_overrides") if isinstance(config, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}

    async def _build_mcp_transport(
        self, manifest: PluginManifest, granted: Permissions, *, plugin_dir: str
    ) -> McpTransport:
        runtime = manifest.runtime
        if runtime.transport == "stdio":
            return StdioMcpTransport(list(runtime.command), cwd=plugin_dir)
        # Streamable HTTP: the url must be inside the egress allowlist (§7 "bound by the
        # manifest egress allowlist"); the OAuth 2.1 bearer token comes from the secret store.
        if self._http_client is None:
            raise RuntimeError("no HTTP client is configured for the MCP http transport")
        url = runtime.url or ""
        if not network_allowed(url, granted.network):
            raise RuntimeError(f"MCP url '{url}' is not permitted by the egress allowlist")
        token = ""
        if runtime.auth_secret:
            token = await self._secrets.get(runtime.auth_secret)
        return HttpMcpTransport(url, client=self._http_client, bearer_token=token)

    def _register_mcp_tools(self, plugin_id: str, tools: list[McpTool]) -> list[str]:
        """Register each discovered tool (write-risk by default, per-tool operator override)
        plus a single ``read_resource`` read tool when the server exposes resources."""
        ctx = self._mcp_ctx[plugin_id]
        ctx.tool_names = {}
        used: set[str] = set()
        names: list[str] = []
        for tool in tools:
            display = self._mcp_display_name(plugin_id, tool.name, used)
            used.add(display)
            risk = ctx.risk_overrides.get(tool.name, MCP_DEFAULT_RISK)
            if risk not in _VALID_RISK:
                risk = MCP_DEFAULT_RISK
            spec = ToolSpec(
                name=display,
                provider=f"mcp:{plugin_id}",
                description=tool.description or f"{plugin_id} — {tool.name}",
                risk=risk,
                required_scopes=tuple(ctx.granted.scopes),
                input_schema=self._mcp_input_schema(tool.input_schema),
                output_schema=dict(_RESULT_SCHEMA),
                executor=self._make_mcp_executor(plugin_id, tool.name),
                timeout_s=ctx.call_timeout_s,
            )
            self._register_spec_lenient(spec)
            ctx.tool_names[display] = tool.name
            names.append(display)
        if ctx.has_resources:
            # Dedupe against the discovered tools: a server MAY advertise a tool literally
            # named 'read_resource', whose display name would otherwise collide with the
            # synthetic resource tool and abort enable.
            read_name = self._mcp_display_name(plugin_id, "read_resource", used)
            self._register_spec_lenient(
                ToolSpec(
                    name=read_name,
                    provider=f"mcp:{plugin_id}",
                    description=f"{plugin_id} — read an MCP resource by URI",
                    risk="read",  # resources are read-only by definition (§7)
                    required_scopes=tuple(ctx.granted.scopes),
                    input_schema=dict(_RESOURCE_INPUT),
                    output_schema=dict(_RESULT_SCHEMA),
                    executor=self._make_resource_executor(plugin_id),
                    timeout_s=ctx.call_timeout_s,
                )
            )
            names.append(read_name)
        return names

    def _register_spec_lenient(self, spec: ToolSpec) -> None:
        """Register a discovered tool; if the server's advertised JSON Schema is rejected,
        fall back to a permissive one so ONE bad tool does not sink the whole server."""
        try:
            self._registry.register(spec)
        except (ValueError, SchemaError):
            logger.warning("mcp.tool_schema_rejected", tool=spec.name, provider=spec.provider)
            self._registry.register(replace(spec, input_schema=dict(_PERMISSIVE_INPUT)))

    def _mcp_display_name(self, plugin_id: str, tool_name: str, used: set[str]) -> str:
        base = f"mcp.{plugin_id}.{_sanitize_tool_segment(tool_name)}"
        name = base
        suffix = 2
        while name in used or self._registry.get(name) is not None:
            name = f"{base}_{suffix}"
            suffix += 1
        return name

    @staticmethod
    def _mcp_input_schema(advertised: dict[str, Any]) -> dict[str, Any]:
        """The server's advertised inputSchema if it is a valid object schema, else a
        permissive one. A structurally-invalid schema (e.g. a typo'd nested ``type``) must
        NOT reach the registry: ``check_schema`` raises ``SchemaError`` (which is NOT a
        ValueError), so the registry's lenient fallback would miss it and the whole server
        would fail to enable. Validate here and downgrade to permissive instead."""
        if not (isinstance(advertised, dict) and advertised.get("type") == "object"):
            return dict(_PERMISSIVE_INPUT)
        try:
            Draft202012Validator.check_schema(advertised)
        except SchemaError:
            logger.warning("mcp.tool_schema_invalid", schema_keys=sorted(advertised))
            return dict(_PERMISSIVE_INPUT)
        return dict(advertised)

    def _make_mcp_executor(self, plugin_id: str, tool_name: str) -> Any:
        async def _execute(
            arguments: dict[str, Any], context: ToolExecutionContext
        ) -> ExecutionOutcome:
            result = await self.call_mcp_tool(plugin_id, tool_name, arguments)
            return ExecutionOutcome(result=result)

        return _execute

    def _make_resource_executor(self, plugin_id: str) -> Any:
        async def _execute(
            arguments: dict[str, Any], context: ToolExecutionContext
        ) -> ExecutionOutcome:
            uri = str(arguments.get("uri", ""))
            return ExecutionOutcome(result=await self.call_mcp_resource(plugin_id, uri))

        return _execute

    async def call_mcp_tool(
        self, plugin_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        client = self._processes.get(plugin_id)
        if not isinstance(client, McpClient):
            raise ToolError("TOOL_UNAVAILABLE", f"MCP server '{plugin_id}' is not enabled")
        try:
            return await client.call_tool(tool_name, arguments)
        except RpcError as exc:
            self._maybe_quarantine(plugin_id, client, exc)
            raise _tool_error_for(exc) from exc

    async def call_mcp_resource(self, plugin_id: str, uri: str) -> dict[str, Any]:
        client = self._processes.get(plugin_id)
        if not isinstance(client, McpClient):
            raise ToolError("TOOL_UNAVAILABLE", f"MCP server '{plugin_id}' is not enabled")
        try:
            return await client.read_resource(uri)
        except RpcError as exc:
            self._maybe_quarantine(plugin_id, client, exc)
            raise _tool_error_for(exc) from exc

    async def _schedule_reregister(self, plugin_id: str) -> None:
        """The list_changed handler: spawn a COALESCED (tracked) re-registration and return
        at once, so the transport's notification task never blocks on the round-trip it
        triggers and a flood of notifications cannot amplify into a re-discovery storm."""
        if plugin_id in self._reregister_active:
            self._reregister_pending.add(plugin_id)  # fold into the in-flight pass
            return
        self._reregister_active.add(plugin_id)
        task = asyncio.ensure_future(self._run_reregister(plugin_id))
        self._mcp_tasks.add(task)
        task.add_done_callback(self._mcp_tasks.discard)

    async def _run_reregister(self, plugin_id: str) -> None:
        try:
            await self._reregister_mcp(plugin_id)
            while plugin_id in self._reregister_pending:  # coalesced notifications: one more
                self._reregister_pending.discard(plugin_id)
                await self._reregister_mcp(plugin_id)
        finally:
            self._reregister_active.discard(plugin_id)
            self._reregister_pending.discard(plugin_id)

    async def _reregister_mcp(self, plugin_id: str) -> None:
        """Re-discover and re-register an MCP server's tools in place (TITAN-INT-050:
        notifications/tools/list_changed without restarting the server or core)."""
        client = self._processes.get(plugin_id)
        if not isinstance(client, McpClient) or plugin_id not in self._mcp_ctx:
            return
        try:
            async with asyncio.timeout(_DISCOVERY_DEADLINE_S):
                tools = await client.list_tools()
                resources = await client.list_resources()
        except (RpcError, TimeoutError):
            return
        # The two awaits above yield: a disable/quarantine (or a re-enable onto a NEW
        # client) may have landed meanwhile. Re-check identity before touching the
        # registry, or we would resurrect tools for a server that is no longer running.
        ctx = self._mcp_ctx.get(plugin_id)
        if ctx is None or self._processes.get(plugin_id) is not client:
            return
        ctx.resources = resources
        self._registry.unregister_provider(f"mcp:{plugin_id}")
        self._tools[plugin_id] = self._register_mcp_tools(plugin_id, tools)
        logger.info("mcp.tools_reregistered", plugin_id=plugin_id, tools=self._tools[plugin_id])

    async def drain_mcp_tasks(self) -> None:
        """Await any in-flight live re-registration so tests observe the new tool set
        deterministically instead of racing the notification."""
        while self._mcp_tasks:
            await asyncio.gather(*list(self._mcp_tasks), return_exceptions=True)

    def mcp_info(self, plugin_id: str) -> dict[str, Any] | None:
        """What the introspection endpoint surfaces for an enabled MCP server — its tools,
        its resources (read via ``mcp.<id>.read_resource``), and its prompts (Vol 2)."""
        ctx = self._mcp_ctx.get(plugin_id)
        if ctx is None:
            return None
        return {
            "protocol_version": ctx.protocol_version,
            "server_info": ctx.server_info,
            "tools": list(self._tools.get(plugin_id, [])),
            "resources": [
                {
                    "uri": r.uri,
                    "name": r.name,
                    "description": r.description,
                    "mime_type": r.mime_type,
                }
                for r in ctx.resources
            ],
            "prompts": [
                {"name": p.name, "description": p.description, "arguments": p.arguments}
                for p in ctx.prompts
            ],
        }

    def _maybe_quarantine(self, plugin_id: str, worker: Worker, exc: RpcError) -> None:
        """Off the caller's error path — quarantine after three consecutive faults, tracked
        so it cannot be GC'd and so aclose()/tests can drain it (TITAN-INT-014)."""
        if worker.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            task = asyncio.create_task(
                self.quarantine(
                    plugin_id,
                    f"{MAX_CONSECUTIVE_ERRORS} consecutive failures; last: {exc.message}",
                )
            )
            self._quarantines.add(task)
            task.add_done_callback(self._quarantines.discard)

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
        await self.drain_mcp_tasks()
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
        # so no separate allowlist state is needed (that global would only leak). A plugin
        # is either python (plugin:<id>) or MCP (mcp:<id>); unloading both covers it.
        removed = self._registry.unregister_provider(f"plugin:{plugin_id}")
        removed += self._registry.unregister_provider(f"mcp:{plugin_id}")
        self._tools.pop(plugin_id, None)
        self._mcp_ctx.pop(plugin_id, None)
        return sorted(removed)

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
        """Proxy one call into a python-plugin worker. Faults become structured
        ToolErrors, and three consecutive unhandled errors auto-quarantine the plugin.
        (MCP servers dispatch through :meth:`call_mcp_tool`.)"""
        process = self._processes.get(plugin_id)
        if not isinstance(process, PluginProcess):
            raise ToolError("TOOL_UNAVAILABLE", f"plugin '{plugin_id}' is not enabled")
        try:
            payload = await process.call(
                jsonrpc.M_TOOLS_CALL, {"name": tool_name, "arguments": arguments}
            )
        except RpcError as exc:
            self._maybe_quarantine(plugin_id, process, exc)
            raise _tool_error_for(exc) from exc
        result = (payload or {}).get("result")
        return result if isinstance(result, dict) else {"result": result}

    async def deliver_webhook(
        self, plugin_id: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        process = self._processes.get(plugin_id)
        if not isinstance(process, PluginProcess):
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
