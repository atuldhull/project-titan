"""Public service interface of the `plugins` module (TITAN-BCK-004; Vol 7 §3,
TITAN-INT-010..017): the manifest-driven plugin lifecycle.

AVAILABLE → INSTALLED (validate, isolated per-plugin virtualenv, pin version+hash)
→ ENABLED (operator grants the declared permissions; healthcheck within 10 s; tools
contributed to the Tool Registry) → QUARANTINED / REMOVED.

Transitions serialize per plugin on a row lock and each is a single transaction, so a
crash mid-transition leaves the previous state intact (TITAN-INT-013). Enable is
all-or-nothing: if the worker fails to start or fails its healthcheck, nothing is
registered and the install stays INSTALLED.

Owns: plugins, plugin_installs. Only this interface may be imported by other modules;
Pydantic v2 DTOs — never SQLAlchemy models — cross this boundary.
"""

import asyncio
import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.gateway.problems import ProblemException, problem_type
from titan.plugins import repository as repo
from titan.plugins.manifest import ManifestError, Permissions, PluginManifest, parse_manifest
from titan.plugins.secrets import ConfigCipher, EnvSecretsProvider, SecretsProvider
from titan.plugins.supervisor import (
    PluginSupervisor,
    build_http_fetcher,
    run_health_monitor,
    venv_python,
)

logger = structlog.get_logger("titan.plugins.service")

MANIFEST_FILENAME = "titan-plugin.yaml"

STATE_INSTALLED = "installed"
STATE_ENABLED = "enabled"
STATE_QUARANTINED = "quarantined"
STATE_REMOVED = "removed"

#: How long a disable lets in-flight tool calls finish before terminating the worker.
_DISABLE_DRAIN_S = 5.0


# --- DTOs --------------------------------------------------------------------


class PluginOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    plugin_id: str
    name: str
    plugin_version: str
    description: str = ""
    origin: str = ""
    content_hash: str
    manifest: dict[str, Any] = Field(default_factory=dict)


class PluginInstallOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    plugin_id: str
    installed_version: str
    state: str
    content_hash: str
    granted_permissions: dict[str, Any] = Field(default_factory=dict)
    #: The installed manifest — the router reads contributes.webhooks / ui_panels from it.
    manifest: dict[str, Any] = Field(default_factory=dict)
    #: Absolute server-side path of the plugin's directory. Needed in-process
    #: (the UI-asset and config-schema routes resolve against it) but EXCLUDED
    #: from serialization: these responses are guarded only by `plugin:read`,
    #: which `*:read` grants to every role INCLUDING every agent, so returning
    #: it published the host's filesystem layout to callers with no other way
    #: to learn it. Attribute access is unaffected — only the JSON changes.
    plugin_dir: str = Field(default="", exclude=True)
    error_count: int = 0
    last_error: str = ""
    tools: list[str] = Field(default_factory=list)


class PermissionDiff(BaseModel):
    """What an upgrade would ADD relative to the operator-approved grant. A non-empty
    diff blocks activation until re-approved (TITAN-INT-017)."""

    widened: bool
    added: list[str] = Field(default_factory=list)


def _bad_request(errors: list[str]) -> ProblemException:
    return ProblemException(
        status=422,
        title="Invalid plugin manifest",
        detail="; ".join(errors),
        problem_type=problem_type("validation"),
    )


def _not_found(plugin_id: str) -> ProblemException:
    return ProblemException(
        status=404,
        title="Unknown plugin",
        detail=f"plugin '{plugin_id}' is not known",
        problem_type=problem_type("not-found"),
    )


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type=problem_type("conflict")
    )


def _plugin_out(row: Row[Any]) -> PluginOut:
    return PluginOut(
        plugin_id=row.plugin_id,
        name=row.name,
        plugin_version=row.plugin_version,
        description=row.description,
        origin=row.origin,
        content_hash=row.content_hash,
        manifest=dict(row.manifest or {}),
    )


def _install_out(row: Row[Any], tools: list[str] | None = None) -> PluginInstallOut:
    return PluginInstallOut(
        plugin_id=row.plugin_id,
        installed_version=row.installed_version,
        state=row.state,
        content_hash=row.content_hash,
        granted_permissions=dict(row.granted_permissions or {}),
        manifest=dict(row.manifest or {}),
        plugin_dir=row.plugin_dir,
        error_count=row.error_count,
        last_error=row.last_error,
        tools=tools or [],
    )


# --- manifest loading + permission diffing -----------------------------------


def load_manifest(plugin_dir: Path, *, known_scopes: frozenset[str]) -> tuple[PluginManifest, str]:
    """Read + validate ``titan-plugin.yaml``; returns the manifest and its content hash.
    Raises :class:`ManifestError` with field-level messages (TITAN-INT-010)."""
    path = plugin_dir / MANIFEST_FILENAME
    if not path.exists():
        raise ManifestError([f"{MANIFEST_FILENAME}: not found in {plugin_dir}"])
    raw_bytes = path.read_bytes()
    try:
        data = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ManifestError([f"{MANIFEST_FILENAME}: invalid YAML — {exc}"]) from exc
    if not isinstance(data, dict):
        raise ManifestError([f"{MANIFEST_FILENAME}: must be a YAML mapping"])
    manifest = parse_manifest(data, known_scopes=known_scopes)
    return manifest, hashlib.sha256(raw_bytes).hexdigest()


def _flatten(permissions: dict[str, Any]) -> set[str]:
    """A permission set → comparable capability strings, so a widening is exact."""
    out: set[str] = set()
    for pattern in permissions.get("network") or []:
        out.add(f"network:{pattern}")
    for name in permissions.get("secrets") or []:
        out.add(f"secrets:{name}")
    for scope in permissions.get("scopes") or []:
        out.add(f"scopes:{scope}")
    for glob in permissions.get("filesystem") or []:
        out.add(f"filesystem:{glob}")
    events = permissions.get("events") or {}
    for topic in events.get("subscribe") or []:
        out.add(f"events.subscribe:{topic}")
    for topic in events.get("publish") or []:
        out.add(f"events.publish:{topic}")
    return out


def diff_permissions(granted: dict[str, Any], requested: dict[str, Any]) -> PermissionDiff:
    """Anything REQUESTED that was never GRANTED is a widening (TITAN-INT-017)."""
    added = sorted(_flatten(requested) - _flatten(granted))
    return PermissionDiff(widened=bool(added), added=added)


# --- virtualenv (never the core environment, §3.3) ---------------------------


async def _create_venv(plugin_dir: Path) -> str:
    """An isolated venv per plugin. With no declared dependencies we build it
    ``--without-pip`` (fast); with a requirements.txt we build a full venv and install
    into it. Either way the interpreter is NOT the core one."""
    venv_dir = plugin_dir / ".venv"
    requirements = plugin_dir / "requirements.txt"
    args = ["-m", "venv", *([] if requirements.exists() else ["--without-pip"]), str(venv_dir)]
    proc = await asyncio.create_subprocess_exec(
        sys.executable, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"venv creation failed: {err.decode('utf-8', 'replace')[:500]}")
    if requirements.exists():
        python = venv_python(str(venv_dir))
        if python is None:
            raise RuntimeError("venv has no interpreter")
        pip = await asyncio.create_subprocess_exec(
            python,
            "-m",
            "pip",
            "install",
            "-r",
            str(requirements),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, perr = await asyncio.wait_for(pip.communicate(), timeout=600)
        if pip.returncode != 0:
            raise RuntimeError(
                f"dependency install failed: {perr.decode('utf-8', 'replace')[:500]}"
            )
    return str(venv_dir)


# --- lifecycle ---------------------------------------------------------------


def _scan_dirs(plugins_dir: Path) -> list[Path]:
    if not plugins_dir.exists():
        return []
    return sorted(p for p in plugins_dir.iterdir() if p.is_dir())


async def discover(
    engine: AsyncEngine, plugins_dir: Path, *, known_scopes: frozenset[str]
) -> list[PluginOut]:
    """Scan ``plugins_dir`` and record the catalogue (AVAILABLE). A directory whose
    manifest does not validate is skipped and logged — one bad plugin must not stop
    discovery of the rest."""
    found: list[PluginOut] = []
    # Filesystem scanning is blocking — keep it off the event loop.
    entries = await asyncio.to_thread(_scan_dirs, plugins_dir)
    for entry in entries:
        try:
            manifest, content_hash = load_manifest(entry, known_scopes=known_scopes)
        except ManifestError as exc:
            logger.warning("plugin.discover_rejected", path=str(entry), errors=exc.errors)
            continue
        async with engine.begin() as conn:
            row = await repo.upsert_plugin(
                conn,
                plugin_id=manifest.id,
                name=manifest.name,
                plugin_version=manifest.version,
                description=manifest.description,
                origin=str(entry),
                content_hash=content_hash,
                manifest=manifest.model_dump(mode="json", by_alias=True),
            )
        found.append(_plugin_out(row))
    return found


async def install(
    engine: AsyncEngine, plugin_id: str, *, known_scopes: frozenset[str]
) -> PluginInstallOut:
    """Validate the manifest, build the isolated venv, pin version + hash. Grants
    NOTHING — enable is a separate, explicit operator act."""
    async with engine.connect() as conn:
        catalogue = await repo.get_plugin(conn, plugin_id)
    if catalogue is None:
        raise _not_found(plugin_id)
    plugin_dir = Path(catalogue.origin)
    try:
        manifest, content_hash = load_manifest(plugin_dir, known_scopes=known_scopes)
    except ManifestError as exc:
        raise _bad_request(exc.errors) from exc
    # A python plugin gets an isolated venv; an MCP server runs its own command (stdio) or
    # is a remote endpoint (http) — neither needs a TITAN-built venv (Vol 7 §7).
    venv = ""
    if manifest.runtime.type == "python":
        try:
            venv = await _create_venv(plugin_dir)
        except (RuntimeError, TimeoutError) as exc:
            raise _bad_request([f"install: {exc}"]) from exc

    async with engine.begin() as conn:
        row = await repo.upsert_install(
            conn,
            plugin_id=manifest.id,
            installed_version=manifest.version,
            content_hash=content_hash,
            manifest=manifest.model_dump(mode="json", by_alias=True),
            venv_path=venv,
            plugin_dir=str(plugin_dir),
        )
    return _install_out(row)


async def enable(
    engine: AsyncEngine,
    supervisor: PluginSupervisor,
    plugin_id: str,
    *,
    known_scopes: frozenset[str],
) -> PluginInstallOut:
    """Grant the manifest's declared permissions, start the worker, healthcheck it, and
    contribute its tools. All-or-nothing: a failed start/healthcheck registers nothing
    and leaves the install INSTALLED (TITAN-INT-013)."""
    async with engine.begin() as conn:
        install_row = await repo.get_install_for_update(conn, plugin_id)
        if install_row is None:
            raise _not_found(plugin_id)
        if install_row.state == STATE_ENABLED:
            raise _conflict(f"plugin '{plugin_id}' is already enabled")
        if install_row.state == STATE_QUARANTINED:
            raise _conflict(
                f"plugin '{plugin_id}' is quarantined — reinstall it before enabling again"
            )
        manifest_data = dict(install_row.manifest or {})
        plugin_dir = install_row.plugin_dir
        venv = install_row.venv_path

    try:
        manifest = parse_manifest(manifest_data, known_scopes=known_scopes)
    except ManifestError as exc:
        raise _bad_request(exc.errors) from exc

    granted: Permissions = manifest.permissions  # the operator approves the declared set
    try:
        await supervisor.start_plugin(
            manifest, granted, plugin_dir=plugin_dir, python_exe=venv_python(venv)
        )
    except Exception as exc:  # noqa: BLE001 — a failed enable must not corrupt state
        async with engine.begin() as conn:
            await repo.set_state(conn, plugin_id, state=STATE_INSTALLED, last_error=str(exc)[:2000])
        raise _conflict(f"enable failed: {exc}") from exc

    try:
        async with engine.begin() as conn:
            row = await repo.set_state(
                conn,
                plugin_id,
                state=STATE_ENABLED,
                granted_permissions=granted.model_dump(mode="json"),
                last_error="",
                error_count=0,
                enabled=True,
            )
    except Exception as exc:  # noqa: BLE001 — the worker is live but the ENABLED commit
        # failed; do NOT leave a split-brain (process running + tools registered, DB
        # saying 'installed'). Tear the worker back down so the persisted state is true.
        await supervisor.stop_plugin(plugin_id)
        async with engine.begin() as conn:
            await repo.set_state(conn, plugin_id, state=STATE_INSTALLED, last_error=str(exc)[:2000])
        raise _conflict(f"enable failed to commit: {exc}") from exc
    return _install_out(row, supervisor.tools_of(plugin_id))


async def disable(
    engine: AsyncEngine, supervisor: PluginSupervisor, plugin_id: str
) -> PluginInstallOut:
    """Revoke permissions and unload tools atomically — in-flight calls drain, and new
    calls are rejected TOOL_UNAVAILABLE because the tool is no longer registered."""
    # Unregister then give in-flight calls a short window to complete before terminating.
    await supervisor.stop_plugin(plugin_id, drain_timeout_s=_DISABLE_DRAIN_S)
    async with engine.begin() as conn:
        if await repo.get_install_for_update(conn, plugin_id) is None:
            raise _not_found(plugin_id)
        row = await repo.set_state(
            conn, plugin_id, state=STATE_INSTALLED, granted_permissions={}, error_count=0
        )
    return _install_out(row)


async def remove(
    engine: AsyncEngine, supervisor: PluginSupervisor, plugin_id: str
) -> PluginInstallOut:
    """Uninstall: unload, drop the venv, retain audit rows."""
    await supervisor.stop_plugin(plugin_id)
    async with engine.begin() as conn:
        install_row = await repo.get_install_for_update(conn, plugin_id)
        if install_row is None:
            raise _not_found(plugin_id)
        venv = install_row.venv_path
        row = await repo.set_state(conn, plugin_id, state=STATE_REMOVED, granted_permissions={})
    if venv:
        await asyncio.to_thread(shutil.rmtree, venv, True)
    return _install_out(row)


async def upgrade(
    engine: AsyncEngine,
    supervisor: PluginSupervisor,
    plugin_id: str,
    *,
    known_scopes: frozenset[str],
) -> PermissionDiff:
    """Re-read the on-disk manifest. If it requests MORE than the operator granted, the
    plugin is left INSTALLED and inactive pending re-approval — activation is blocked
    until an operator explicitly re-enables it (TITAN-INT-017). A non-widening upgrade
    re-enables in place."""
    async with engine.connect() as conn:
        install_row = await repo.get_install(conn, plugin_id)
    if install_row is None:
        raise _not_found(plugin_id)
    was_enabled = install_row.state == STATE_ENABLED
    try:
        manifest, content_hash = load_manifest(
            Path(install_row.plugin_dir), known_scopes=known_scopes
        )
    except ManifestError as exc:
        raise _bad_request(exc.errors) from exc

    diff = diff_permissions(
        dict(install_row.granted_permissions or {}), manifest.permissions.model_dump(mode="json")
    )
    # Re-pin the new version/hash. upsert_install resets state to INSTALLED, so a widened
    # upgrade is inactive by construction until an operator re-enables (= re-approves).
    await supervisor.stop_plugin(plugin_id)
    async with engine.begin() as conn:
        # Serialize against concurrent enable/disable on the same row (INT-013).
        if await repo.get_install_for_update(conn, plugin_id) is None:
            raise _not_found(plugin_id)
        await repo.upsert_install(
            conn,
            plugin_id=manifest.id,
            installed_version=manifest.version,
            content_hash=content_hash,
            manifest=manifest.model_dump(mode="json", by_alias=True),
            venv_path=install_row.venv_path,
            plugin_dir=install_row.plugin_dir,
        )
    if not diff.widened and was_enabled:
        await enable(engine, supervisor, plugin_id, known_scopes=known_scopes)
    return diff


async def configure(
    engine: AsyncEngine, plugin_id: str, config: dict[str, Any], *, cipher: ConfigCipher
) -> PluginInstallOut:
    """Persist operator settings, encrypted at rest (SEC-063). Secrets are NEVER stored
    here — the manifest names them and the runtime injects their values."""
    async with engine.begin() as conn:
        if await repo.get_install_for_update(conn, plugin_id) is None:
            raise _not_found(plugin_id)
        row = await repo.set_config(conn, plugin_id, blob=cipher.encrypt(config))
    return _install_out(row)


async def get_config(
    conn: AsyncConnection, plugin_id: str, *, cipher: ConfigCipher
) -> dict[str, Any]:
    row = await repo.get_install(conn, plugin_id)
    if row is None:
        raise _not_found(plugin_id)
    return cipher.decrypt(row.config_encrypted)


async def list_catalogue(conn: AsyncConnection) -> list[PluginOut]:
    return [_plugin_out(r) for r in await repo.list_plugins(conn)]


async def list_installs(conn: AsyncConnection) -> list[PluginInstallOut]:
    return [_install_out(r) for r in await repo.list_installs(conn)]


async def get_install(
    conn: AsyncConnection, plugin_id: str, supervisor: PluginSupervisor | None = None
) -> PluginInstallOut:
    row = await repo.get_install(conn, plugin_id)
    if row is None:
        raise _not_found(plugin_id)
    tools = supervisor.tools_of(plugin_id) if supervisor is not None else []
    return _install_out(row, tools)


async def resume_enabled(
    engine: AsyncEngine, supervisor: PluginSupervisor, *, known_scopes: frozenset[str]
) -> list[str]:
    """On boot, restart the workers of every plugin the operator left ENABLED. One that
    fails to come back is quarantined rather than silently missing its tools."""
    async with engine.connect() as conn:
        rows = await repo.list_installs(conn, state=STATE_ENABLED)
    resumed: list[str] = []
    for row in rows:
        try:
            manifest = parse_manifest(dict(row.manifest or {}), known_scopes=known_scopes)
            granted = Permissions.model_validate(dict(row.granted_permissions or {}))
            await supervisor.start_plugin(
                manifest, granted, plugin_dir=row.plugin_dir, python_exe=venv_python(row.venv_path)
            )
            resumed.append(row.plugin_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugin.resume_failed", plugin_id=row.plugin_id, error=str(exc))
            await supervisor.quarantine(row.plugin_id, f"failed to resume on boot: {exc}")
    return resumed


__all__ = [
    "MANIFEST_FILENAME",
    "STATE_ENABLED",
    "STATE_INSTALLED",
    "STATE_QUARANTINED",
    "STATE_REMOVED",
    # Re-exported so the composition root (main.py) touches only this boundary
    # (TITAN-BCK-004: routers/repositories/runtime internals stay private).
    "ConfigCipher",
    "EnvSecretsProvider",
    "PluginSupervisor",
    "SecretsProvider",
    "build_http_fetcher",
    "run_health_monitor",
    "PermissionDiff",
    "PluginInstallOut",
    "PluginOut",
    "configure",
    "diff_permissions",
    "disable",
    "discover",
    "enable",
    "get_config",
    "get_install",
    "install",
    "list_catalogue",
    "list_installs",
    "load_manifest",
    "remove",
    "resume_enabled",
    "upgrade",
]
