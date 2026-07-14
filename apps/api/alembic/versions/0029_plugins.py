"""The manifest-driven plugin runtime (Phase 52; Vol 7 §3, TITAN-INT-010..017).

Two tables:

* ``plugins`` — the discovered catalogue (AVAILABLE): origin + content hash + manifest.
* ``plugin_installs`` — the lifecycle record for the ONE active install of a plugin id
  (§3.6 forbids two simultaneously-active versions ⇒ plugin_id is UNIQUE). It stores
  the operator-GRANTED permission set separately from the manifest's REQUESTED set, so
  an upgrade that widens permissions is detectable and re-enters approval (INT-017).
  ``config_encrypted`` holds operator settings as an AES-256-GCM blob with a
  key-version tag (SEC-063) — secrets themselves are NEVER stored here, only named.

Both are mutable, so they carry the TITAN-BCK-008 convention columns and the shared
set_updated_at trigger.

Hand-written (autogenerate is forbidden).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: None = None
depends_on: None = None


def _conventions() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    ]


def _updated_at_trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def upgrade() -> None:
    op.create_table(
        "plugins",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("plugin_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        # The manifest SemVer — distinct from the convention `version` int below.
        sa.Column("plugin_version", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("origin", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("manifest", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_conventions(),
        sa.UniqueConstraint("plugin_id", name="uq_plugins_plugin_id"),
    )
    _updated_at_trigger("plugins")

    op.create_table(
        "plugin_installs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("plugin_id", sa.Text, nullable=False),
        sa.Column("installed_version", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default=sa.text("'installed'")),
        sa.Column("manifest", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "granted_permissions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("config_encrypted", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("venv_path", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("plugin_dir", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("error_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("quarantined_at", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("enabled_at", TIMESTAMP(timezone=True), nullable=True),
        *_conventions(),
        # One active install per plugin id (§3.6: no two versions simultaneously active).
        sa.UniqueConstraint("plugin_id", name="uq_plugin_installs_plugin_id"),
        sa.CheckConstraint(
            "state IN ('installed', 'enabled', 'quarantined', 'removed')",
            name="ck_plugin_installs_state",
        ),
    )
    op.create_index("ix_plugin_installs_state", "plugin_installs", ["state"])
    _updated_at_trigger("plugin_installs")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_plugin_installs_updated_at ON plugin_installs")
    op.drop_index("ix_plugin_installs_state", table_name="plugin_installs")
    op.drop_table("plugin_installs")
    op.execute("DROP TRIGGER IF EXISTS trg_plugins_updated_at ON plugins")
    op.drop_table("plugins")
