"""The plugin runtime end to end (Phase 52; Vol 7 §3.3–3.5, TITAN-INT-013..017).

Real subprocesses, real Postgres. Proves the DoD:

* a sample JSON-RPC plugin installs, enables, and contributes a tool VIA MANIFEST ONLY,
  and its calls are gated + audit-logged by the Phase-19 Tool Bus (no parallel path);
* the conformance suite attempts one violation per permission class — all five fail
  closed with PERMISSION_DENIED;
* a crashing (``os._exit(1)``) and a hanging plugin both leave core serving, returning
  structured errors; three consecutive failures auto-quarantine, unregistering its tools;
* an upgrade that widens permissions is blocked pending re-approval;
* a plugin tool can never launder an agent into scopes it does not hold.
"""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.gateway.rbac import KNOWN_SCOPES
from titan.plugins import service as plugins
from titan.plugins.secrets import StaticSecretsProvider
from titan.plugins.supervisor import PluginSupervisor
from titan.tools.service import ToolError, ToolRegistry, build_registry

pytestmark = pytest.mark.schema

_SECRETS = StaticSecretsProvider({"SAMPLE_TOKEN": "s3cret", "HOOK": "hook-key"})

_MANIFEST = """\
manifest_version: 1
id: titan.probe
name: Probe
version: 1.0.0
titan_api: ">=1.0,<2.0"
runtime:
  type: python
  entrypoint: probe_plugin:ProbePlugin
permissions:
  network: ["https://api.example.com"]
  secrets: [SAMPLE_TOKEN]
  events:
    publish: [integration.probe.ok]
  scopes: [task:read]
  filesystem: ["reports/*.md"]
contributes:
  tools:
    - name: probe.echo
      risk: read
    - name: probe.try
      risk: read
  webhooks:
    - path: /hooks/probe
      verify: hmac-sha256
      secret: SAMPLE_TOKEN
"""

_PLUGIN = """\
class ProbePlugin:
    id = "titan.probe"

    def __init__(self, capabilities=None):
        self.caps = capabilities

    def health(self):
        return True

    def call_tool(self, name, arguments):
        if name == "probe.echo":
            return {"echo": arguments}
        if name == "probe.try":
            cap = arguments.get("capability")
            arg = arguments.get("argument")
            if cap == "secrets":
                return {"v": self.caps.secret(arg)}
            if cap == "network":
                return {"v": self.caps.fetch(arg)}
            if cap == "events":
                self.caps.publish(arg, {})
                return {"v": "published"}
            if cap == "filesystem":
                return {"v": self.caps.read_file(arg)}
            if cap == "scopes":
                return {"v": self.caps._call("titan/scope.check", {"scope": arg})}
        raise ValueError("unknown tool " + str(name))

    def on_webhook(self, path, body):
        return {"path": path, "received": body}
"""

_CRASHER = """\
import os

class Crasher:
    id = "titan.crash"
    def health(self):
        return True
    def call_tool(self, name, arguments):
        os._exit(1)          # hard crash — must NOT take core down
"""

_HANGER = """\
import time

class Hanger:
    id = "titan.hang"
    def health(self):
        return True
    def call_tool(self, name, arguments):
        time.sleep(600)      # hang forever — must NOT block core
"""

_FLAKY = """\
class Flaky:
    id = "titan.flaky"
    def health(self):
        return True
    def call_tool(self, name, arguments):
        raise RuntimeError("boom")   # unhandled every time → 3 strikes → quarantine
"""

_FLIP = """\
class Flip:
    id = "titan.flip"
    def __init__(self, capabilities=None):
        self._calls = 0
    def health(self):
        self._calls += 1
        return self._calls <= 1      # healthy at enable, unhealthy thereafter
    def call_tool(self, name, arguments):
        return {"ok": True}
"""


def _write_plugin(root: Path, name: str, manifest: str, source: str, module: str) -> Path:
    plugin_dir = root / name
    (plugin_dir / "ui").mkdir(parents=True, exist_ok=True)
    (plugin_dir / "titan-plugin.yaml").write_text(manifest, encoding="utf-8")
    (plugin_dir / f"{module}.py").write_text(source, encoding="utf-8")
    (plugin_dir / "ui" / "panel.html").write_text("<h1>panel</h1>", encoding="utf-8")
    (plugin_dir / "reports").mkdir(exist_ok=True)
    (plugin_dir / "reports" / "ok.md").write_text("# report", encoding="utf-8")
    return plugin_dir


@pytest.fixture
def probe_dir(tmp_path: Path) -> Path:
    _write_plugin(tmp_path, "probe", _MANIFEST, _PLUGIN, "probe_plugin")
    return tmp_path


def _supervisor(engine: Any, registry: ToolRegistry, root: Path | None = None) -> PluginSupervisor:
    return PluginSupervisor(
        engine, registry, secrets=_SECRETS, call_timeout_s=5.0, workspace_root=root
    )


async def _enable_from_dir(
    engine: Any, supervisor: PluginSupervisor, plugins_dir: Path, plugin_id: str
) -> Any:
    """discover → install (real per-plugin venv) → enable."""
    await plugins.discover(engine, plugins_dir, known_scopes=KNOWN_SCOPES)
    await plugins.install(engine, plugin_id, known_scopes=KNOWN_SCOPES)
    return await plugins.enable(engine, supervisor, plugin_id, known_scopes=KNOWN_SCOPES)


# --- lifecycle (TITAN-INT-013) ----------------------------------------------


def test_lifecycle_install_enable_disable_remove(migrated_engine: Any, probe_dir: Path) -> None:
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry, probe_dir / "probe")

    async def scenario() -> dict[str, Any]:
        catalogue = await plugins.discover(migrated_engine, probe_dir, known_scopes=KNOWN_SCOPES)
        installed = await plugins.install(migrated_engine, "titan.probe", known_scopes=KNOWN_SCOPES)
        enabled = await plugins.enable(
            migrated_engine, supervisor, "titan.probe", known_scopes=KNOWN_SCOPES
        )
        registered = registry.get("probe.echo") is not None
        disabled = await plugins.disable(migrated_engine, supervisor, "titan.probe")
        after_disable = registry.get("probe.echo") is None
        removed = await plugins.remove(migrated_engine, supervisor, "titan.probe")
        return {
            "catalogue": [p.plugin_id for p in catalogue],
            "installed": installed.state,
            "enabled": enabled.state,
            "tools": enabled.tools,
            "registered": registered,
            "disabled": disabled.state,
            "unloaded": after_disable,
            "removed": removed.state,
        }

    out = run_async(scenario)
    assert out["catalogue"] == ["titan.probe"]
    assert out["installed"] == "installed"  # install grants nothing
    assert out["enabled"] == "enabled"
    assert set(out["tools"]) == {"probe.echo", "probe.try"}
    assert out["registered"] is True  # contributed via manifest only
    assert out["disabled"] == "installed"
    assert out["unloaded"] is True  # tools unloaded atomically on disable
    assert out["removed"] == "removed"


def test_tool_call_routes_through_the_plugin_and_returns_its_result(
    migrated_engine: Any, probe_dir: Path
) -> None:
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry, probe_dir / "probe")

    async def scenario() -> dict[str, Any]:
        await _enable_from_dir(migrated_engine, supervisor, probe_dir, "titan.probe")
        result = await supervisor.call_tool("titan.probe", "probe.echo", {"hello": "world"})
        await supervisor.aclose()
        return result

    assert run_async(scenario) == {"echo": {"hello": "world"}}


def test_plugin_tool_is_a_registry_entry_with_plugin_provider(
    migrated_engine: Any, probe_dir: Path
) -> None:
    """It must be an ORDINARY registry entry so the Phase-19 bus gates + audits it —
    there is deliberately no parallel dispatch path."""
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry, probe_dir / "probe")

    async def scenario() -> Any:
        await _enable_from_dir(migrated_engine, supervisor, probe_dir, "titan.probe")
        spec = registry.get("probe.echo")
        await supervisor.aclose()
        return spec

    spec = run_async(scenario)
    assert spec is not None
    assert spec.provider == "plugin:titan.probe"
    assert spec.risk == "read"
    assert spec.required_scopes == ("task:read",)  # the operator-granted scopes


# --- permission conformance: one violation per class (TITAN-INT-015) --------


@pytest.mark.parametrize(
    ("capability", "argument"),
    [
        ("secrets", "NOT_GRANTED_SECRET"),
        ("network", "https://evil.test/steal"),
        ("events", "integration.not.granted"),
        ("filesystem", "../../etc/passwd"),
        ("scopes", "project:delete"),
    ],
)
def test_every_permission_class_fails_closed(
    migrated_engine: Any, probe_dir: Path, capability: str, argument: str
) -> None:
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry, probe_dir / "probe")

    async def scenario() -> tuple[str, list[tuple[str, str]]]:
        await _enable_from_dir(migrated_engine, supervisor, probe_dir, "titan.probe")
        try:
            await supervisor.call_tool(
                "titan.probe", "probe.try", {"capability": capability, "argument": argument}
            )
            code = "NOT_DENIED"
        except ToolError as exc:
            code = exc.code
        broker = supervisor.broker_of("titan.probe")
        denials = list(broker.denials) if broker else []
        await supervisor.aclose()
        return code, denials

    code, denials = run_async(scenario)
    assert code == "PERMISSION_DENIED", f"{capability} was not denied"
    assert any(d[0] == capability for d in denials), f"{capability} denial not recorded"


def test_repeated_permission_denials_do_not_quarantine_a_good_plugin(
    migrated_engine: Any, probe_dir: Path
) -> None:
    """A PERMISSION_DENIED is the security model working — it must NOT count toward the
    3-strike auto-quarantine, or a well-behaved plugin that probes an ungranted
    capability a few times would be wrongly killed (TITAN-INT-014 is about faults)."""
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry, probe_dir / "probe")

    async def scenario() -> dict[str, Any]:
        await _enable_from_dir(migrated_engine, supervisor, probe_dir, "titan.probe")
        for _ in range(5):
            with pytest.raises(ToolError) as exc:
                await supervisor.call_tool(
                    "titan.probe", "probe.try", {"capability": "secrets", "argument": "NOPE"}
                )
            assert exc.value.code == "PERMISSION_DENIED"
        await supervisor.drain_quarantines()
        still_enabled = "titan.probe" in supervisor.running()
        # A subsequent GRANTED call still works — the plugin was never quarantined.
        ok = await supervisor.call_tool("titan.probe", "probe.echo", {"n": 1})
        await supervisor.aclose()
        return {"running": still_enabled, "echo": ok}

    out = run_async(scenario)
    assert out["running"] is True
    assert out["echo"] == {"echo": {"n": 1}}


def test_a_granted_capability_succeeds(migrated_engine: Any, probe_dir: Path) -> None:
    """Deny-by-default is not deny-everything: the DECLARED secret resolves."""
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry, probe_dir / "probe")

    async def scenario() -> dict[str, Any]:
        await _enable_from_dir(migrated_engine, supervisor, probe_dir, "titan.probe")
        out = await supervisor.call_tool(
            "titan.probe", "probe.try", {"capability": "secrets", "argument": "SAMPLE_TOKEN"}
        )
        await supervisor.aclose()
        return out

    assert run_async(scenario) == {"v": "s3cret"}


# --- sandbox: crash + hang do not take core down (TITAN-INT-016) ------------


def test_a_crashing_plugin_returns_a_structured_error_and_core_survives(
    migrated_engine: Any, tmp_path: Path
) -> None:
    manifest = _MANIFEST.replace("titan.probe", "titan.crash").replace(
        "probe_plugin:ProbePlugin", "crash_plugin:Crasher"
    )
    _write_plugin(tmp_path, "crash", manifest, _CRASHER, "crash_plugin")
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> str:
        await _enable_from_dir(migrated_engine, supervisor, tmp_path, "titan.crash")
        try:
            await supervisor.call_tool("titan.crash", "probe.echo", {})
            code = "NO_ERROR"
        except ToolError as exc:
            code = exc.code
        # Core is still serving: a fresh DB round-trip works after the crash.
        async with migrated_engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
        await supervisor.aclose()
        return code

    assert run_async(scenario) in {"TOOL_UNAVAILABLE", "UPSTREAM_ERROR"}


def test_a_hanging_plugin_times_out_and_core_survives(migrated_engine: Any, tmp_path: Path) -> None:
    manifest = _MANIFEST.replace("titan.probe", "titan.hang").replace(
        "probe_plugin:ProbePlugin", "hang_plugin:Hanger"
    )
    _write_plugin(tmp_path, "hang", manifest, _HANGER, "hang_plugin")
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> str:
        await _enable_from_dir(migrated_engine, supervisor, tmp_path, "titan.hang")
        try:
            await supervisor.call_tool("titan.hang", "probe.echo", {})
            code = "NO_ERROR"
        except ToolError as exc:
            code = exc.code
        async with migrated_engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
        await supervisor.aclose()
        return code

    assert run_async(scenario) == "TIMEOUT"


# --- auto-quarantine after three strikes (TITAN-INT-014) --------------------


def test_three_consecutive_failures_quarantine_and_unregister_tools(
    migrated_engine: Any, tmp_path: Path
) -> None:
    manifest = _MANIFEST.replace("titan.probe", "titan.flaky").replace(
        "probe_plugin:ProbePlugin", "flaky_plugin:Flaky"
    )
    _write_plugin(tmp_path, "flaky", manifest, _FLAKY, "flaky_plugin")
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> dict[str, Any]:
        await _enable_from_dir(migrated_engine, supervisor, tmp_path, "titan.flaky")
        for _ in range(3):
            with pytest.raises(ToolError):
                await supervisor.call_tool("titan.flaky", "probe.echo", {})
        # The 3rd failure fires the quarantine AUTOMATICALLY off the error path; drain
        # it so the terminal state is observed deterministically (we do NOT quarantine
        # by hand here — that would mask whether the auto-quarantine actually ran).
        await supervisor.drain_quarantines()
        async with migrated_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT state FROM plugin_installs WHERE plugin_id = 'titan.flaky'")
                )
            ).one()
            events = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM outbox "
                        "WHERE payload->'data'->>'type' = 'plugin.quarantined'"
                    )
                )
            ).scalar_one()
        await supervisor.aclose()
        return {
            "state": row.state,
            "events": int(events),
            "tool_gone": registry.get("probe.echo") is None,
        }

    out = run_async(scenario)
    assert out["state"] == "quarantined"
    assert out["events"] >= 1  # plugin.quarantined on the domain stream
    assert out["tool_gone"] is True  # tools unregistered → further calls TOOL_UNAVAILABLE


# --- a failed healthcheck auto-quarantines (TITAN-INT-014, the OTHER trigger) --


def test_a_healthcheck_that_starts_failing_is_quarantined_by_the_monitor(
    migrated_engine: Any, tmp_path: Path
) -> None:
    """INT-014 quarantines on 'three unhandled exceptions OR a failed healthcheck'. This
    covers the healthcheck half: a plugin healthy at enable that later fails health is
    quarantined by the periodic sweep, not only by call errors."""
    manifest = _MANIFEST.replace("titan.probe", "titan.flip").replace(
        "probe_plugin:ProbePlugin", "flip_plugin:Flip"
    )
    _write_plugin(tmp_path, "flip", manifest, _FLIP, "flip_plugin")
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry)

    async def scenario() -> dict[str, Any]:
        await _enable_from_dir(migrated_engine, supervisor, tmp_path, "titan.flip")
        enabled_before = "titan.flip" in supervisor.running()
        quarantined = await supervisor.sweep_health()  # health() now returns False
        async with migrated_engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT state FROM plugin_installs WHERE plugin_id = 'titan.flip'")
                )
            ).one()
        await supervisor.aclose()
        return {
            "enabled_before": enabled_before,
            "quarantined": quarantined,
            "state": row.state,
            "tool_gone": registry.get("probe.echo") is None,
        }

    out = run_async(scenario)
    assert out["enabled_before"] is True
    assert out["quarantined"] == ["titan.flip"]
    assert out["state"] == "quarantined"
    assert out["tool_gone"] is True


# --- upgrade widening re-enters approval (TITAN-INT-017) --------------------


def test_an_upgrade_that_widens_permissions_is_blocked_pending_reapproval(
    migrated_engine: Any, probe_dir: Path
) -> None:
    registry = build_registry()
    supervisor = _supervisor(migrated_engine, registry, probe_dir / "probe")

    async def scenario() -> dict[str, Any]:
        await _enable_from_dir(migrated_engine, supervisor, probe_dir, "titan.probe")
        # Widen the on-disk manifest: add a network pattern that was never granted.
        path = probe_dir / "probe" / "titan-plugin.yaml"
        path.write_text(
            _MANIFEST.replace(
                'network: ["https://api.example.com"]',
                'network: ["https://api.example.com", "https://newhost.test"]',
            ),
            encoding="utf-8",
        )
        diff = await plugins.upgrade(
            migrated_engine, supervisor, "titan.probe", known_scopes=KNOWN_SCOPES
        )
        async with migrated_engine.connect() as conn:
            install = await plugins.get_install(conn, "titan.probe")
        await supervisor.aclose()
        return {
            "widened": diff.widened,
            "added": diff.added,
            "state": install.state,
            "inactive": registry.get("probe.echo") is None,
        }

    out = run_async(scenario)
    assert out["widened"] is True
    assert "network:https://newhost.test" in out["added"]
    assert out["state"] == "installed"  # NOT enabled — activation blocked
    assert out["inactive"] is True  # its tools are not live until re-approved


# --- no privilege laundering (ADR-INT-04) -----------------------------------


def test_a_plugin_tool_never_self_grants_its_scopes_to_an_agent() -> None:
    """A plugin tool's required_scopes must NOT flow into the calling agent's authority:
    otherwise granting the tool would mint the very scope the bus then checks."""
    from titan.runtime.grants import granted_scopes
    from titan.tools.service import ExecutionOutcome, ToolExecutionContext, ToolSpec

    async def _exec(a: dict[str, Any], c: ToolExecutionContext) -> ExecutionOutcome:
        return ExecutionOutcome(result={})

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="evil.escalate",
            provider="plugin:titan.evil",
            description="",
            risk="read",
            required_scopes=("project:delete",),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            executor=_exec,
        )
    )
    registry.register(
        ToolSpec(
            name="core.reader",
            provider="builtin:core",
            description="",
            risk="read",
            required_scopes=("task:read",),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            executor=_exec,
        )
    )
    scopes = granted_scopes(registry, ("evil.escalate", "core.reader"))
    assert "task:read" in scopes  # core tools DO confer their scopes
    assert "project:delete" not in scopes  # the plugin tool confers NOTHING


def test_registry_unregisters_by_provider() -> None:
    async def _exec(a: dict[str, Any], c: Any) -> Any:
        return None

    from titan.tools.service import ToolSpec

    registry = ToolRegistry()
    for name in ("p.one", "p.two"):
        registry.register(
            ToolSpec(
                name=name,
                provider="plugin:titan.x",
                description="",
                risk="read",
                required_scopes=(),
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                executor=_exec,
            )
        )
    assert registry.unregister_provider("plugin:titan.x") == ["p.one", "p.two"]
    assert registry.get("p.one") is None
    # Re-registering after unregister must not raise (re-enable path).
    registry.register(
        ToolSpec(
            name="p.one",
            provider="plugin:titan.x",
            description="",
            risk="read",
            required_scopes=(),
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            executor=_exec,
        )
    )
    assert registry.get("p.one") is not None
