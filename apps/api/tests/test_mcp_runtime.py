"""The MCP client end to end (Phase 53; Vol 7 §7, TITAN-INT-050, ADR-INT-02).

Real Postgres, a real spawned MCP server (the deterministic stdlib reference server, which
stands in for @modelcontextprotocol/server-filesystem so CI stays hermetic). Proves the DoD:

* an MCP server installs + enables VIA MANIFEST ONLY and its tools appear as ``mcp.<id>.*``;
* those tools execute under the full Phase-19 Tool Bus (schema, authz, audit) — no parallel
  path — and a ``PERMISSION_DENIED`` fires when the calling agent lacks the required scope;
* ``notifications/tools/list_changed`` re-registers the tool set live, no restart;
* every MCP tool is ``write`` risk unless the operator downgrades it per tool in config.
"""

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from titan.gateway.rbac import KNOWN_SCOPES
from titan.plugins import service as plugins
from titan.plugins.secrets import ConfigCipher, StaticSecretsProvider
from titan.plugins.supervisor import PluginSupervisor
from titan.tools import repository as audit_repo
from titan.tools.bus import ToolBus, ToolCaller
from titan.tools.envelopes import new_call
from titan.tools.service import ToolRegistry, build_registry

pytestmark = pytest.mark.schema

_REF_SERVER = str(Path(__file__).parent / "mcp_servers" / "reference_stdio.py")
_PLUGIN_ID = "titan.ref_mcp"
_CIPHER = ConfigCipher("mcp-test-key")


def _write_manifest(root: Path) -> Path:
    plugin_dir = root / "ref"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 1,
        "id": _PLUGIN_ID,
        "name": "Reference MCP",
        "version": "1.0.0",
        "titan_api": ">=1.0,<2.0",
        "runtime": {
            "type": "mcp",
            "transport": "stdio",
            # yaml.safe_dump quotes the interpreter path so Windows backslashes survive.
            "command": [sys.executable, _REF_SERVER],
        },
        "permissions": {"scopes": ["workspace:read"]},
    }
    (plugin_dir / "titan-plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return plugin_dir


def _supervisor(engine: AsyncEngine, registry: ToolRegistry) -> PluginSupervisor:
    return PluginSupervisor(
        engine,
        registry,
        secrets=StaticSecretsProvider({}),
        config_cipher=_CIPHER,
        call_timeout_s=10.0,
    )


async def _enable(engine: AsyncEngine, supervisor: PluginSupervisor, plugins_dir: Path) -> Any:
    await plugins.discover(engine, plugins_dir, known_scopes=KNOWN_SCOPES)
    await plugins.install(engine, _PLUGIN_ID, known_scopes=KNOWN_SCOPES)
    return await plugins.enable(engine, supervisor, _PLUGIN_ID, known_scopes=KNOWN_SCOPES)


def _caller(scopes: frozenset[str], project_id: uuid.UUID, task_id: uuid.UUID) -> ToolCaller:
    return ToolCaller(
        agent_id="agent.backend.1",
        role="backend",
        project_id=project_id,
        granted_scopes=scopes,
        task_id=task_id,
        initiator_chain=("user:founder",),
    )


# --- installs + registers via manifest only (DoD) ---------------------------


def test_mcp_server_registers_its_tools_via_manifest_only(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> dict[str, Any]:
        plugins_dir = _write_manifest(tmp_path).parent
        enabled = await _enable(migrated_engine, supervisor, plugins_dir)
        add = registry.get(f"mcp.{_PLUGIN_ID}.add")
        read_resource = registry.get(f"mcp.{_PLUGIN_ID}.read_resource")
        info = supervisor.mcp_info(_PLUGIN_ID)
        await supervisor.aclose()
        return {
            "state": enabled.state,
            "tools": sorted(enabled.tools),
            "add_provider": add.provider if add else None,
            "add_risk": add.risk if add else None,
            "add_scopes": add.required_scopes if add else None,
            "resource_tool_risk": read_resource.risk if read_resource else None,
            "protocol": (info or {}).get("protocol_version"),
            "resources": [r["uri"] for r in (info or {}).get("resources", [])],
            "prompts": [p["name"] for p in (info or {}).get("prompts", [])],
        }

    out = run_async(scenario)
    assert out["state"] == "enabled"
    assert f"mcp.{_PLUGIN_ID}.add" in out["tools"]
    assert f"mcp.{_PLUGIN_ID}.read_resource" in out["tools"]
    assert out["add_provider"] == f"mcp:{_PLUGIN_ID}"
    assert out["add_risk"] == "write"  # write-by-default (ADR-INT-02)
    assert out["add_scopes"] == ("workspace:read",)  # operator-granted scopes
    assert out["resource_tool_risk"] == "read"  # resources are read-only
    assert out["protocol"] in {"2025-06-18", "2025-03-26", "2024-11-05"}
    assert out["resources"] == ["mem://note"]
    assert out["prompts"] == ["greet"]


# --- executes under the bus with audit; scope gate fires (DoD) --------------


def test_mcp_tool_runs_through_the_bus_and_denies_when_scope_is_missing(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> dict[str, Any]:
        plugins_dir = _write_manifest(tmp_path).parent
        await _enable(migrated_engine, supervisor, plugins_dir)
        bus = ToolBus(registry, migrated_engine)
        project_id, task_id = uuid.uuid4(), uuid.uuid4()
        tool = f"mcp.{_PLUGIN_ID}.add"

        # An agent that HOLDS workspace:read → the call runs and is audited.
        ok_caller = _caller(frozenset({"workspace:read"}), project_id, task_id)
        ok_call = new_call(
            tool,
            {"a": 4, "b": 6},
            project_id=project_id,
            agent_id="agent.backend.1",
            task_id=task_id,
        )
        ok = await bus.invoke(ok_call, ok_caller)
        async with migrated_engine.connect() as conn:
            ok_rows = await audit_repo.rows_for_call(conn, ok_call.tool_call_id)

        # An agent LACKING the scope → PERMISSION_DENIED, and the rejection is audited too.
        no_caller = _caller(frozenset(), project_id, task_id)
        no_call = new_call(
            tool,
            {"a": 1, "b": 2},
            project_id=project_id,
            agent_id="agent.backend.1",
            task_id=task_id,
        )
        denied = await bus.invoke(no_call, no_caller)
        await supervisor.aclose()
        return {
            "ok_status": ok.status,
            "ok_result": ok.result,
            "ok_audit": [r.status for r in ok_rows],
            "denied_code": denied.code,
        }

    out = run_async(scenario)
    assert out["ok_status"] == "ok"
    assert out["ok_result"]["structuredContent"] == {"sum": 10.0}
    assert out["ok_audit"] == ["started", "ok"]  # full audit pair on the MCP path
    assert out["denied_code"] == "PERMISSION_DENIED"


# --- live re-registration on notifications/tools/list_changed (DoD) ----------


def test_list_changed_reregisters_the_tool_set_live(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> dict[str, Any]:
        plugins_dir = _write_manifest(tmp_path).parent
        await _enable(migrated_engine, supervisor, plugins_dir)
        late = f"mcp.{_PLUGIN_ID}.late_tool"
        before = registry.get(late) is not None
        # 'grow' makes the server advertise a new tool AND emit tools/list_changed.
        await supervisor.call_mcp_tool(_PLUGIN_ID, "grow", {})
        await supervisor.drain_mcp_tasks()  # await the (async) re-registration
        after = registry.get(late) is not None
        await supervisor.aclose()
        return {"before": before, "after": after}

    out = run_async(scenario)
    assert out["before"] is False  # not advertised at enable
    assert out["after"] is True  # re-registered live, no restart


# --- operator per-tool risk downgrade (ADR-INT-02) --------------------------


def test_operator_can_downgrade_a_tool_risk_in_config(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> dict[str, str | None]:
        plugins_dir = _write_manifest(tmp_path).parent
        await plugins.discover(migrated_engine, plugins_dir, known_scopes=KNOWN_SCOPES)
        await plugins.install(migrated_engine, _PLUGIN_ID, known_scopes=KNOWN_SCOPES)
        # The operator downgrades the read-only 'echo' tool BEFORE enabling.
        await plugins.configure(
            migrated_engine, _PLUGIN_ID, {"risk_overrides": {"echo": "read"}}, cipher=_CIPHER
        )
        await plugins.enable(migrated_engine, supervisor, _PLUGIN_ID, known_scopes=KNOWN_SCOPES)
        echo = registry.get(f"mcp.{_PLUGIN_ID}.echo")
        add = registry.get(f"mcp.{_PLUGIN_ID}.add")
        await supervisor.aclose()
        return {"echo_risk": echo.risk if echo else None, "add_risk": add.risk if add else None}

    out = run_async(scenario)
    assert out["echo_risk"] == "read"  # downgraded by the operator
    assert out["add_risk"] == "write"  # untouched tools keep the write default


# --- a fault isolates + core survives ---------------------------------------


def test_a_faulting_mcp_tool_returns_a_structured_error(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> dict[str, Any]:
        plugins_dir = _write_manifest(tmp_path).parent
        await _enable(migrated_engine, supervisor, plugins_dir)
        bus = ToolBus(registry, migrated_engine)
        project_id, task_id = uuid.uuid4(), uuid.uuid4()
        caller = _caller(frozenset({"workspace:read"}), project_id, task_id)
        call = new_call(
            f"mcp.{_PLUGIN_ID}.boom",
            {},
            project_id=project_id,
            agent_id="agent.backend.1",
            task_id=task_id,
        )
        result = await bus.invoke(call, caller)
        # Core still serves after the tool fault.
        async with migrated_engine.connect() as conn:
            alive = (await conn.execute(text("SELECT 1"))).scalar_one()
        await supervisor.aclose()
        return {"code": result.code, "alive": alive}

    out = run_async(scenario)
    assert out["code"] == "UPSTREAM_ERROR"  # the server's JSON-RPC error, structured
    assert out["alive"] == 1


def test_a_malformed_tool_schema_does_not_sink_the_whole_server(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A server advertising a structurally-invalid inputSchema (check_schema raises
    SchemaError, NOT ValueError) must still enable: that one tool registers with a
    permissive schema instead of aborting the entire server."""
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> dict[str, Any]:
        plugins_dir = _write_manifest(tmp_path).parent
        enabled = await _enable(migrated_engine, supervisor, plugins_dir)
        bad = registry.get(f"mcp.{_PLUGIN_ID}.bad_schema")
        good = registry.get(f"mcp.{_PLUGIN_ID}.add")
        await supervisor.aclose()
        return {
            "state": enabled.state,
            "bad_registered": bad is not None,
            "bad_schema": bad.input_schema if bad else None,
            "good_registered": good is not None,
        }

    out = run_async(scenario)
    assert out["state"] == "enabled"  # the server enabled despite the bad tool
    assert out["bad_registered"] is True
    assert out["bad_schema"] == {"type": "object", "additionalProperties": True}  # permissive
    assert out["good_registered"] is True  # sibling tools are unaffected


def test_three_consecutive_faults_quarantine_the_mcp_server(
    migrated_engine: AsyncEngine, tmp_path: Path
) -> None:
    """The MCP path inherits the TITAN-INT-014 auto-quarantine: three consecutive transport
    faults unload the server and persist QUARANTINED (a PERMISSION_DENIED would not)."""
    import contextlib

    from titan.tools.service import ToolError

    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> dict[str, Any]:
        plugins_dir = _write_manifest(tmp_path).parent
        await _enable(migrated_engine, supervisor, plugins_dir)
        for _ in range(3):
            with contextlib.suppress(ToolError):
                await supervisor.call_mcp_tool(_PLUGIN_ID, "boom", {})
        await supervisor.drain_quarantines()
        async with migrated_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT state FROM plugin_installs WHERE plugin_id = :p"),
                    {"p": _PLUGIN_ID},
                )
            ).one()
        tool_gone = registry.get(f"mcp.{_PLUGIN_ID}.add") is None
        await supervisor.aclose()
        return {"state": row.state, "tool_gone": tool_gone}

    out = run_async(scenario)
    assert out["state"] == "quarantined"
    assert out["tool_gone"] is True  # tools unloaded → further calls TOOL_UNAVAILABLE
