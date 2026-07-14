"""Plugin registry persistence (Vol 7 §3.3). Private to the module — only ``service``
crosses the boundary.

* ``plugins`` — the DISCOVERED catalogue (the AVAILABLE stage): what was found on
  disk / at a Git URL, with its origin and content hash.
* ``plugin_installs`` — the lifecycle record for the one active install of a plugin
  id (§3.6 forbids two simultaneously-active versions, so ``plugin_id`` is UNIQUE).
  It carries the operator-GRANTED permission set separately from the manifest's
  REQUESTED set, which is what lets an upgrade that widens permissions be detected and
  re-enter approval (TITAN-INT-017).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, uuid7


class Plugin(Base):
    """A discovered plugin (AVAILABLE). Discovery is idempotent on ``plugin_id``."""

    __tablename__ = "plugins"
    __table_args__ = (UniqueConstraint("plugin_id", name="uq_plugins_plugin_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    #: Reverse-DNS id from the manifest, e.g. 'titan.jira'.
    plugin_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    #: The manifest's SemVer. Named distinctly from the TITAN-BCK-008 convention
    #: ``version`` column (an int optimistic lock), which every table carries.
    plugin_version: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    #: Where it came from — a local directory, a Git URL, an OCI ref.
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    #: sha256 over the manifest bytes — the integrity handle the install pins.
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class PluginInstall(Base):
    """The lifecycle state of the single active install of a plugin id."""

    __tablename__ = "plugin_installs"
    __table_args__ = (UniqueConstraint("plugin_id", name="uq_plugin_installs_plugin_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    plugin_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: The pinned SemVer + content hash this install was validated against.
    installed_version: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # installed | enabled | quarantined | removed
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'installed'"))
    #: The manifest as installed (the REQUESTED permission set lives inside).
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: The permission set the operator actually APPROVED. A later manifest that
    #: requests more than this is a widening and must re-enter approval (INT-017).
    granted_permissions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    #: Operator settings from config_schema — AES-256-GCM at rest (SEC-063).
    config_encrypted: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    #: The isolated per-plugin virtualenv — never the core environment.
    venv_path: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    plugin_dir: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    #: Consecutive unhandled errors; three auto-quarantines (TITAN-INT-014).
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    quarantined_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
