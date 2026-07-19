"""Plugin Manager additions for Vol 4 §6.12 (Phase 64).

Phase 52/53 built the lifecycle for an operator with shell access. These are the
four things a SCREEN needs that did not exist: the config schema as data, a
readable (redacted) config, secret status, and MCP add-by-URL/command.

The load-bearing test in this file is
:func:`test_a_saved_secret_never_appears_in_any_response_body`, which is half of
TITAN-FRT-035's acceptance criterion.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from titan.gateway.rbac import KNOWN_SCOPES
from titan.plugins.manager import (
    SECRET_SENTINEL,
    McpServerSpec,
    declares_config_schema,
    manifest_document,
    redact_config,
    secret_keys_of,
    strip_sentinels,
    validate_spec,
    write_mcp_manifest,
)
from titan.plugins.manifest import ManifestError
from titan.plugins.service import PluginInstallOut

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace": {"type": "string"},
        "api_token": {"type": "string", "writeOnly": True},
        "legacy_password": {"type": "string", "format": "password"},
        "verify_tls": {"type": "boolean"},
    },
}


# --- the config schema, as data ---------------------------------------------


def test_write_only_and_password_are_both_treated_as_secret() -> None:
    # `writeOnly` is JSON Schema's own vocabulary for "never present in a
    # response" (draft 2019-09 §9.4); `format: password` is what most authors
    # reach for first. Honouring only one of them would leak the other.
    assert secret_keys_of(_SCHEMA) == ["api_token", "legacy_password"]


def test_a_plugin_with_no_schema_has_no_secrets() -> None:
    assert secret_keys_of(None) == []
    assert secret_keys_of({"type": "object"}) == []


def test_redaction_masks_saved_secrets_and_leaves_the_rest() -> None:
    redacted = redact_config(
        {"workspace": "acme", "api_token": "real-token-value", "verify_tls": True},
        ["api_token"],
    )
    assert redacted["api_token"] == SECRET_SENTINEL
    assert redacted["workspace"] == "acme"
    assert redacted["verify_tls"] is True
    assert "real-token-value" not in json.dumps(redacted)


def test_an_unset_secret_stays_absent_rather_than_masked() -> None:
    # Rendering •••• for a field that was never set tells the operator a secret
    # exists where none does — so they leave it alone believing it configured.
    assert redact_config({"workspace": "acme"}, ["api_token"]) == {"workspace": "acme"}


def test_submitting_the_mask_back_keeps_the_stored_secret() -> None:
    # THE trap: the form renders •••• for a saved secret, so an unchanged form
    # submits the sentinel. Storing it verbatim replaces a working credential
    # with four bullet characters — a config that looks right and fails to
    # authenticate.
    merged = strip_sentinels(
        {"workspace": "acme-2", "api_token": SECRET_SENTINEL},
        {"workspace": "acme", "api_token": "real-token-value"},
        ["api_token"],
    )
    assert merged["api_token"] == "real-token-value"
    assert merged["workspace"] == "acme-2"


def test_a_key_the_form_never_rendered_survives_a_save() -> None:
    # `configure` REPLACES the stored document, so a fold is the only thing
    # stopping an unrenderable field from being deleted — including
    # `risk_overrides`, which the SUPERVISOR reads (per-tool MCP risk
    # downgrades) and which no config schema declares, so no form will ever
    # submit it back.
    merged = strip_sentinels(
        {"workspace": "acme"},
        {"workspace": "old", "risk_overrides": {"tool.a": "read"}},
        [],
    )
    assert merged["risk_overrides"] == {"tool.a": "read"}
    assert merged["workspace"] == "acme", "a submitted value still wins"


def test_a_genuinely_new_secret_replaces_the_old_one() -> None:
    merged = strip_sentinels(
        {"api_token": "rotated-value"}, {"api_token": "old-value"}, ["api_token"]
    )
    assert merged["api_token"] == "rotated-value"


def test_the_mask_submitted_for_a_secret_that_was_never_set_is_dropped() -> None:
    # Otherwise the literal string "••••" becomes the credential.
    merged = strip_sentinels({"api_token": SECRET_SENTINEL}, {}, ["api_token"])
    assert "api_token" not in merged


def test_an_unloadable_schema_masks_EVERY_value_rather_than_leaking() -> None:
    # FAIL CLOSED. The schema is the only thing that says which keys are
    # secret, so when a plugin DECLARES one that cannot be read — a syntax
    # error, a rename, a path failing containment — an empty key list would
    # mean "nothing here is secret" and every stored credential would be
    # returned verbatim.
    saved = {"workspace": "acme", "api_token": "sk-live-do-not-leak"}
    assert redact_config(saved, [], mask_all=True) == {
        "workspace": SECRET_SENTINEL,
        "api_token": SECRET_SENTINEL,
    }
    # And the unmasked path is genuinely different, so the test is not vacuous.
    assert redact_config(saved, [])["api_token"] == "sk-live-do-not-leak"


def test_declares_config_schema_distinguishes_absent_from_unreadable() -> None:
    declared = PluginInstallOut(
        plugin_id="x",
        installed_version="1",
        state="installed",
        content_hash="h",
        manifest={"config_schema": "config.schema.json"},
    )
    silent = PluginInstallOut(
        plugin_id="y",
        installed_version="1",
        state="installed",
        content_hash="h",
        manifest={},
    )
    # Both load_config_schema to None; only the first is a FAILURE.
    assert declares_config_schema(declared) is True
    assert declares_config_schema(silent) is False


def test_a_config_schema_escaping_the_plugin_directory_is_refused(tmp_path: Path) -> None:
    # The manifest is plugin-authored, so its config_schema path is untrusted.
    from titan.plugins.manager import load_config_schema

    outside = tmp_path / "secret.json"
    outside.write_text('{"type": "object"}', encoding="utf-8")
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()

    install = PluginInstallOut(
        plugin_id="titan.evil",
        installed_version="1.0.0",
        state="installed",
        content_hash="x",
        manifest={"config_schema": "../secret.json"},
        plugin_dir=str(plugin_dir),
    )
    assert load_config_schema(install) is None


def test_the_real_sample_plugin_declares_a_loadable_schema() -> None:
    """The fixture plugin the DoD names must actually parse."""
    from titan.llm.config import resolve_config_path
    from titan.plugins.manager import load_config_schema

    plugin_dir = resolve_config_path("plugins/first_party/sample/titan-plugin.yaml").parent
    manifest = yaml.safe_load((plugin_dir / "titan-plugin.yaml").read_text(encoding="utf-8"))
    install = PluginInstallOut(
        plugin_id=manifest["id"],
        installed_version=manifest["version"],
        state="installed",
        content_hash="x",
        manifest=manifest,
        plugin_dir=str(plugin_dir),
    )
    schema = load_config_schema(install)
    assert schema is not None, "the sample plugin's config_schema must resolve"
    assert schema["type"] == "object"
    # It declares a write-only field, so the masking path is exercisable
    # end-to-end against a real fixture rather than only a synthetic one.
    assert "api_token" in secret_keys_of(schema)


def test_the_sample_plugin_declares_a_panel_in_a_named_slot() -> None:
    """The other half of the DoD: a panel, mounted at a named slot."""
    from titan.llm.config import resolve_config_path

    path = resolve_config_path("plugins/first_party/sample/titan-plugin.yaml")
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    panels = manifest["contributes"]["ui_panels"]
    assert panels, "the fixture plugin must contribute a panel"
    assert panels[0]["mount"] == "project.sidebar"
    assert panels[0]["type"] == "iframe"
    assert panels[0]["src"].startswith("/plugin-ui/")


# --- MCP add-by-URL/command --------------------------------------------------


def _http_spec(**over: Any) -> McpServerSpec:
    base: dict[str, Any] = {
        "plugin_id": "acme.docs",
        "name": "Acme Docs",
        "transport": "http",
        "url": "https://mcp.acme.test/v1",
        "network": ["https://mcp.acme.test"],
    }
    base.update(over)
    return McpServerSpec(**base)


def test_an_http_spec_becomes_a_valid_manifest() -> None:
    manifest = validate_spec(_http_spec(), known_scopes=KNOWN_SCOPES)
    assert manifest.runtime.type == "mcp"
    assert manifest.runtime.transport == "http"
    assert manifest.runtime.url == "https://mcp.acme.test/v1"


def test_a_stdio_spec_becomes_a_valid_manifest() -> None:
    manifest = validate_spec(
        McpServerSpec(
            plugin_id="acme.files",
            name="Files",
            transport="stdio",
            command=["npx", "-y", "@modelcontextprotocol/server-filesystem", "."],
        ),
        known_scopes=KNOWN_SCOPES,
    )
    assert manifest.runtime.transport == "stdio"
    assert manifest.runtime.command[0] == "npx"


def test_an_http_spec_without_a_url_is_rejected() -> None:
    # Validated by the REAL parse_manifest, so an add is refused by exactly the
    # rules an install would apply, rather than writing a directory that then
    # fails to discover.
    with pytest.raises(ManifestError):
        validate_spec(_http_spec(url=None), known_scopes=KNOWN_SCOPES)


def test_a_stdio_spec_without_a_command_is_rejected() -> None:
    with pytest.raises(ManifestError):
        validate_spec(
            McpServerSpec(plugin_id="acme.files", name="Files", transport="stdio"),
            known_scopes=KNOWN_SCOPES,
        )


def test_an_unknown_scope_is_rejected_at_add_time() -> None:
    with pytest.raises(ManifestError):
        validate_spec(_http_spec(scopes=["nonsense:scope"]), known_scopes=KNOWN_SCOPES)


@pytest.mark.parametrize(
    "bad_id",
    ["../escape", "UPPER.case", "a", "with space", "semi;colon", "sub/dir"],
)
def test_a_plugin_id_that_could_escape_its_directory_is_rejected(bad_id: str) -> None:
    # The id becomes a DIRECTORY NAME, so it is validated before it is ever
    # joined to a path.
    with pytest.raises((ValueError, ManifestError)):
        validate_spec(_http_spec(plugin_id=bad_id), known_scopes=KNOWN_SCOPES)


def test_an_auth_secret_is_declared_in_permissions_automatically() -> None:
    # The manifest validator requires auth_secret to appear in
    # permissions.secrets. Deriving it means the operator does not discover the
    # omission at enable time.
    document = manifest_document(_http_spec(auth_secret="ACME_TOKEN"))
    assert document["permissions"]["secrets"] == ["ACME_TOKEN"]
    validate_spec(_http_spec(auth_secret="ACME_TOKEN"), known_scopes=KNOWN_SCOPES)


def test_writing_a_manifest_produces_a_discoverable_directory(tmp_path: Path) -> None:
    target = write_mcp_manifest(tmp_path, _http_spec())
    assert (target / "titan-plugin.yaml").is_file()
    written = yaml.safe_load((target / "titan-plugin.yaml").read_text(encoding="utf-8"))
    assert written["id"] == "acme.docs"
    # It is an ORDINARY plugin directory, so the Phase-52 lifecycle applies to
    # it unchanged rather than MCP servers acquiring a parallel one.
    assert written["runtime"]["type"] == "mcp"


def test_adding_the_same_server_twice_is_refused(tmp_path: Path) -> None:
    write_mcp_manifest(tmp_path, _http_spec())
    with pytest.raises(FileExistsError):
        write_mcp_manifest(tmp_path, _http_spec())


# --- the HTTP surface --------------------------------------------------------


@pytest.mark.schema
class TestPluginManagerApi:
    def test_the_plugin_routes_503_when_the_runtime_is_disabled(self, api: dict[str, Any]) -> None:
        # TITAN_PLUGINS_ENABLED defaults to False, so this is the state of a
        # DEFAULT stack. The Plugin Manager must render it as a legitimate
        # "runtime disabled" screen, not as a crash.
        response = api["client"].post("/api/v1/plugins:discover", headers=api["token_for"]("admin"))
        assert response.status_code == 503
        assert "not enabled on this deployment" in response.json()["detail"]

    def test_the_catalogue_still_lists_with_the_runtime_disabled(self, api: dict[str, Any]) -> None:
        # list_catalogue reads the DB directly and never touches the supervisor
        # (unlike every lifecycle route, which 503s), so the Installed and
        # Catalog tabs still render with the runtime off. That 200 is the whole
        # assertion — NOT the row count: `migrated_engine` is session-scoped, so
        # by the time this runs other test files' discover calls have left rows
        # in `plugins`. Asserting `== []` passed in isolation and failed
        # in-suite, which is the recurring shape of this bug (Phase 63 too).
        response = api["client"].get("/api/v1/plugins", headers=api["token_for"]("admin"))
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_installs_never_leak_the_server_filesystem_path(self, api: dict[str, Any]) -> None:
        # plugin:read is reachable by every role INCLUDING every agent, so a
        # response carrying the host's absolute paths published the filesystem
        # layout to callers with no other way to learn it.
        response = api["client"].get("/api/v1/plugins/installs", headers=api["token_for"]("viewer"))
        assert response.status_code == 200
        assert "plugin_dir" not in response.text

    def test_the_install_dto_still_carries_the_path_in_process(self) -> None:
        # Excluded from serialization, NOT removed: the UI-asset and
        # config-schema routes both resolve against it.
        install = PluginInstallOut(
            plugin_id="x",
            installed_version="1",
            state="installed",
            content_hash="h",
            plugin_dir="/srv/plugins/x",
        )
        assert install.plugin_dir == "/srv/plugins/x"
        assert "plugin_dir" not in install.model_dump()

    def test_reading_config_needs_admin_not_read(self, api: dict[str, Any]) -> None:
        # Even masked, a plugin's config names internal hosts, repos and
        # channels — and plugin:read reaches every agent.
        response = api["client"].get(
            "/api/v1/plugins/titan.sample/config", headers=api["token_for"]("viewer")
        )
        assert response.status_code == 403

    def test_adding_an_mcp_server_needs_admin(self, api: dict[str, Any]) -> None:
        response = api["client"].post(
            "/api/v1/plugins:add-mcp",
            headers=api["token_for"]("maintainer"),
            json=_http_spec().model_dump(),
        )
        assert response.status_code == 403


# --- TITAN-FRT-035, end to end against a live runtime ------------------------

_SECRET_VALUE = "sk-live-plugin-secret-DO-NOT-LEAK"

_CONFIGURABLE_MANIFEST = """\
manifest_version: 1
id: titan.configurable
name: Configurable
version: 1.0.0
titan_api: ">=1.0,<2.0"
config_schema: config.schema.json
runtime:
  type: python
  entrypoint: configurable_plugin:ConfigurablePlugin
permissions:
  scopes: [task:read]
contributes:
  ui_panels:
    - id: configurable-panel
      title: Configurable
      mount: project.sidebar
      type: iframe
      src: /plugin-ui/titan.configurable/panel.html
"""

_CONFIGURABLE_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "workspace": {"type": "string", "title": "Workspace"},
            "api_token": {"type": "string", "title": "API token", "writeOnly": True},
        },
        "required": ["workspace"],
    }
)

_CONFIGURABLE_PLUGIN = """\
class ConfigurablePlugin:
    id = "titan.configurable"

    def health(self):
        return True
"""


@pytest.fixture
def configurable_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    plugin_dir = tmp_path / "configurable"
    (plugin_dir / "ui").mkdir(parents=True)
    (plugin_dir / "titan-plugin.yaml").write_text(_CONFIGURABLE_MANIFEST, encoding="utf-8")
    (plugin_dir / "config.schema.json").write_text(_CONFIGURABLE_SCHEMA, encoding="utf-8")
    (plugin_dir / "configurable_plugin.py").write_text(_CONFIGURABLE_PLUGIN, encoding="utf-8")
    (plugin_dir / "ui" / "panel.html").write_text("<h1>Panel</h1>", encoding="utf-8")
    monkeypatch.setenv("TITAN_PLUGINS_ENABLED", "true")
    monkeypatch.setenv("TITAN_PLUGINS_DIR", str(tmp_path))
    # A DETERMINISTIC config key. Left empty, ConfigCipher derives a fresh
    # random key per instance ("ephemeral dev key"), so anything that decrypts
    # through a second cipher — including this file's read-back assertion —
    # fails with InvalidTag rather than a wrong value.
    monkeypatch.setenv("TITAN_PLUGIN_CONFIG_KEY", "phase64-test-key")
    return tmp_path


@pytest.mark.schema
def test_the_config_form_is_served_from_the_plugins_own_schema(
    configurable_plugin: Path, api: dict[str, Any]
) -> None:
    """TITAN-FRT-035, first half: the form is GENERATED, never hand-built."""
    admin = api["token_for"]("admin")
    assert api["client"].post("/api/v1/plugins:discover", headers=admin).status_code == 200
    assert (
        api["client"].post("/api/v1/plugins/titan.configurable/install", headers=admin).status_code
        == 200
    )

    body = api["client"].get("/api/v1/plugins/titan.configurable/config", headers=admin).json()
    assert body["config_schema"]["properties"]["workspace"]["title"] == "Workspace"
    assert body["secret_keys"] == ["api_token"]
    assert body["values"] == {}


@pytest.mark.schema
def test_a_saved_secret_never_appears_in_any_response_body(
    configurable_plugin: Path, api: dict[str, Any]
) -> None:
    """TITAN-FRT-035's acceptance criterion, asserted as a network capture.

    Every response the Plugin Manager can produce for this plugin is collected
    and searched for the literal secret. This is the assertion that would catch
    a future DTO gaining a ``config`` field, or a debug echo, or the value
    riding along inside an error message.
    """
    admin = api["token_for"]("admin")
    api["client"].post("/api/v1/plugins:discover", headers=admin)
    api["client"].post("/api/v1/plugins/titan.configurable/install", headers=admin)

    saved = api["client"].put(
        "/api/v1/plugins/titan.configurable/config",
        headers=admin,
        json={"config": {"workspace": "acme", "api_token": _SECRET_VALUE}},
    )
    assert saved.status_code == 200

    config = api["client"].get("/api/v1/plugins/titan.configurable/config", headers=admin)
    # It reads as •••• — the operator can see a secret IS set...
    assert config.json()["values"]["api_token"] == SECRET_SENTINEL
    assert config.json()["values"]["workspace"] == "acme"

    # ...and the value itself is in NONE of these bodies.
    captured = [
        saved,
        config,
        api["client"].get("/api/v1/plugins", headers=admin),
        api["client"].get("/api/v1/plugins/installs", headers=admin),
        api["client"].get("/api/v1/plugins/titan.configurable", headers=admin),
        api["client"].get("/api/v1/plugins/titan.configurable/health", headers=admin),
    ]
    for response in captured:
        assert _SECRET_VALUE not in response.text, f"secret leaked from {response.url}"


@pytest.mark.schema
def test_resaving_the_form_unchanged_preserves_the_secret(
    configurable_plugin: Path, api: dict[str, Any]
) -> None:
    """The round-trip that would otherwise destroy a working credential."""
    from titan.plugins import service as plugin_service

    admin = api["token_for"]("admin")
    api["client"].post("/api/v1/plugins:discover", headers=admin)
    api["client"].post("/api/v1/plugins/titan.configurable/install", headers=admin)
    api["client"].put(
        "/api/v1/plugins/titan.configurable/config",
        headers=admin,
        json={"config": {"workspace": "acme", "api_token": _SECRET_VALUE}},
    )

    # The operator edits only the workspace and saves; the token field still
    # holds the mask the form rendered.
    api["client"].put(
        "/api/v1/plugins/titan.configurable/config",
        headers=admin,
        json={"config": {"workspace": "acme-two", "api_token": SECRET_SENTINEL}},
    )

    cipher = api["client"].app.state.plugin_cipher

    async def stored() -> dict[str, Any]:
        # The FIXTURE engine, not app.state.engine: `run_async` drives its own
        # event loop, and the app's engine holds connections bound to the
        # TestClient's loop.
        async with api["engine"].connect() as conn:
            return await plugin_service.get_config(conn, "titan.configurable", cipher=cipher)

    from tests.conftest import run_async

    config: dict[str, Any] = run_async(stored)
    assert config["workspace"] == "acme-two"
    assert config["api_token"] == _SECRET_VALUE, "the mask must not overwrite the real secret"


@pytest.mark.schema
def test_health_reports_the_live_supervisor_not_a_stale_column(
    configurable_plugin: Path, api: dict[str, Any]
) -> None:
    # There is no health COLUMN — plugin_installs records lifecycle state and an
    # error counter, not liveness — so "running" has to come from the supervisor.
    admin = api["token_for"]("admin")
    api["client"].post("/api/v1/plugins:discover", headers=admin)
    api["client"].post("/api/v1/plugins/titan.configurable/install", headers=admin)

    installed = api["client"].get("/api/v1/plugins/titan.configurable/health", headers=admin).json()
    assert installed["state"] == "installed"
    assert installed["running"] is False, "installed is not running"
