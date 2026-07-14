"""Private data access for the plugin registry. SQLAlchemy Core over an
``AsyncConnection``; only ``service`` is importable across the module boundary."""

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7

_PLUGIN_COLS = (
    "id, plugin_id, name, plugin_version, description, origin, content_hash, manifest, "
    "created_at, updated_at, version"
)
_INSTALL_COLS = (
    "id, plugin_id, installed_version, content_hash, state, manifest, granted_permissions, "
    "config_encrypted, venv_path, plugin_dir, error_count, last_error, quarantined_at, "
    "enabled_at, created_at, updated_at, version"
)


# --- discovered catalogue ----------------------------------------------------


async def upsert_plugin(
    conn: AsyncConnection,
    *,
    plugin_id: str,
    name: str,
    plugin_version: str,
    description: str,
    origin: str,
    content_hash: str,
    manifest: dict[str, Any],
) -> Row[Any]:
    """Discovery is idempotent: re-scanning refreshes the catalogue entry in place."""
    result = await conn.execute(
        text(
            "INSERT INTO plugins (id, plugin_id, name, plugin_version, description, origin, "
            "content_hash, manifest) "
            "VALUES (:id, :pid, :name, :ver, :desc, :origin, :hash, CAST(:manifest AS jsonb)) "
            "ON CONFLICT (plugin_id) DO UPDATE SET "
            "name = EXCLUDED.name, plugin_version = EXCLUDED.plugin_version, "
            "description = EXCLUDED.description, origin = EXCLUDED.origin, "
            "content_hash = EXCLUDED.content_hash, manifest = EXCLUDED.manifest, "
            "version = plugins.version + 1 "
            f"RETURNING {_PLUGIN_COLS}"
        ),
        {
            "id": uuid7(),
            "pid": plugin_id,
            "name": name,
            "ver": plugin_version,
            "desc": description,
            "origin": origin,
            "hash": content_hash,
            "manifest": json.dumps(manifest),
        },
    )
    return result.one()


async def list_plugins(conn: AsyncConnection) -> Sequence[Row[Any]]:
    result = await conn.execute(text(f"SELECT {_PLUGIN_COLS} FROM plugins ORDER BY plugin_id"))
    return result.all()


async def get_plugin(conn: AsyncConnection, plugin_id: str) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_PLUGIN_COLS} FROM plugins WHERE plugin_id = :pid"), {"pid": plugin_id}
    )
    return result.one_or_none()


# --- installs ----------------------------------------------------------------


async def get_install(conn: AsyncConnection, plugin_id: str) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_INSTALL_COLS} FROM plugin_installs WHERE plugin_id = :pid"),
        {"pid": plugin_id},
    )
    return result.one_or_none()


async def get_install_for_update(conn: AsyncConnection, plugin_id: str) -> Row[Any] | None:
    """Row-locked read — lifecycle transitions serialize on this so a concurrent
    enable/disable cannot interleave (TITAN-INT-013)."""
    result = await conn.execute(
        text(f"SELECT {_INSTALL_COLS} FROM plugin_installs WHERE plugin_id = :pid FOR UPDATE"),
        {"pid": plugin_id},
    )
    return result.one_or_none()


async def list_installs(conn: AsyncConnection, *, state: str | None = None) -> Sequence[Row[Any]]:
    result = await conn.execute(
        text(
            f"SELECT {_INSTALL_COLS} FROM plugin_installs "
            "WHERE (CAST(:state AS text) IS NULL OR state = :state) ORDER BY plugin_id"
        ),
        {"state": state},
    )
    return result.all()


async def upsert_install(
    conn: AsyncConnection,
    *,
    plugin_id: str,
    installed_version: str,
    content_hash: str,
    manifest: dict[str, Any],
    venv_path: str,
    plugin_dir: str,
) -> Row[Any]:
    """Install (or re-install) pins the version + hash and resets the lifecycle to
    ``installed`` with NO permissions granted — enable is a separate, explicit act."""
    result = await conn.execute(
        text(
            "INSERT INTO plugin_installs (id, plugin_id, installed_version, content_hash, "
            "state, manifest, granted_permissions, venv_path, plugin_dir) "
            "VALUES (:id, :pid, :ver, :hash, 'installed', CAST(:manifest AS jsonb), "
            "CAST('{}' AS jsonb), :venv, :dir) "
            "ON CONFLICT (plugin_id) DO UPDATE SET "
            "installed_version = EXCLUDED.installed_version, "
            "content_hash = EXCLUDED.content_hash, manifest = EXCLUDED.manifest, "
            "state = 'installed', venv_path = EXCLUDED.venv_path, "
            "plugin_dir = EXCLUDED.plugin_dir, error_count = 0, last_error = '', "
            "version = plugin_installs.version + 1 "
            f"RETURNING {_INSTALL_COLS}"
        ),
        {
            "id": uuid7(),
            "pid": plugin_id,
            "ver": installed_version,
            "hash": content_hash,
            "manifest": json.dumps(manifest),
            "venv": venv_path,
            "dir": plugin_dir,
        },
    )
    return result.one()


async def set_state(
    conn: AsyncConnection,
    plugin_id: str,
    *,
    state: str,
    granted_permissions: dict[str, Any] | None = None,
    last_error: str | None = None,
    error_count: int | None = None,
    enabled: bool = False,
    quarantined: bool = False,
) -> Row[Any]:
    result = await conn.execute(
        text(
            "UPDATE plugin_installs SET state = :state, "
            "granted_permissions = COALESCE(CAST(:grants AS jsonb), granted_permissions), "
            "last_error = COALESCE(CAST(:err AS text), last_error), "
            "error_count = COALESCE(CAST(:count AS integer), error_count), "
            "enabled_at = CASE WHEN :enabled THEN now() ELSE enabled_at END, "
            "quarantined_at = CASE WHEN :quarantined THEN now() ELSE quarantined_at END, "
            "version = version + 1 "
            f"WHERE plugin_id = :pid RETURNING {_INSTALL_COLS}"
        ),
        {
            "pid": plugin_id,
            "state": state,
            "grants": json.dumps(granted_permissions) if granted_permissions is not None else None,
            "err": last_error,
            "count": error_count,
            "enabled": enabled,
            "quarantined": quarantined,
        },
    )
    return result.one()


async def set_config(conn: AsyncConnection, plugin_id: str, *, blob: str) -> Row[Any]:
    result = await conn.execute(
        text(
            "UPDATE plugin_installs SET config_encrypted = :blob, version = version + 1 "
            f"WHERE plugin_id = :pid RETURNING {_INSTALL_COLS}"
        ),
        {"pid": plugin_id, "blob": blob},
    )
    return result.one()


async def delete_install(conn: AsyncConnection, plugin_id: str) -> None:
    await conn.execute(
        text("DELETE FROM plugin_installs WHERE plugin_id = :pid"), {"pid": plugin_id}
    )


async def project_owner(conn: AsyncConnection) -> uuid.UUID | None:
    """An operator to notify on auto-quarantine. Plugins are host-wide (not
    project-scoped), so the notification goes to the earliest project owner — the
    closest thing to an installation operator this schema has."""
    result = await conn.execute(text("SELECT created_by FROM projects ORDER BY created_at LIMIT 1"))
    row = result.one_or_none()
    return uuid.UUID(str(row.created_by)) if row is not None else None
