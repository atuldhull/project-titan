"""The Plugin Manager REST surface, webhooks, and UI panels (Phase 52; Vol 7 §3.2/§3.3,
TITAN-INT-011). Schema-marked — real DB, real plugin subprocess.

Proves the remaining DoD clauses: the lifecycle is driven entirely over REST under the
``plugin:admin`` permission, the sample plugin's UI panel serves at ``/plugin-ui/<id>/``
with no core code changes, and a webhook is accepted only with a valid HMAC signature.
"""

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.schema

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
  secrets: [HOOK_SECRET]
  scopes: [task:read]
contributes:
  tools:
    - name: probe.echo
      risk: read
  ui_panels:
    - id: probe-panel
      title: Probe
      mount: project.sidebar
      type: iframe
      src: /plugin-ui/titan.probe/panel.html
  webhooks:
    - path: /hooks/probe
      verify: hmac-sha256
      secret: HOOK_SECRET
"""

_PLUGIN = """\
class ProbePlugin:
    id = "titan.probe"

    def __init__(self, capabilities=None):
        self.caps = capabilities

    def health(self):
        return True

    def call_tool(self, name, arguments):
        return {"echo": arguments}

    def on_webhook(self, path, body):
        return {"path": path, "received": body}
"""

_HOOK_SECRET = "hook-key"


@pytest.fixture
def plugin_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a plugin on disk and turn the runtime on BEFORE the app is built."""
    plugin_dir = tmp_path / "probe"
    (plugin_dir / "ui").mkdir(parents=True)
    (plugin_dir / "titan-plugin.yaml").write_text(_MANIFEST, encoding="utf-8")
    (plugin_dir / "probe_plugin.py").write_text(_PLUGIN, encoding="utf-8")
    (plugin_dir / "ui" / "panel.html").write_text("<h1>Probe panel</h1>", encoding="utf-8")
    monkeypatch.setenv("TITAN_PLUGINS_ENABLED", "true")
    monkeypatch.setenv("TITAN_PLUGINS_DIR", str(tmp_path))
    monkeypatch.setenv("TITAN_PLUGIN_SECRET_HOOK_SECRET", _HOOK_SECRET)
    return tmp_path


def _enable(api: dict[str, Any]) -> None:
    admin = api["token_for"]("admin")
    assert api["client"].post("/api/v1/plugins:discover", headers=admin).status_code == 200
    assert (
        api["client"].post("/api/v1/plugins/titan.probe/install", headers=admin).status_code == 200
    )
    resp = api["client"].post("/api/v1/plugins/titan.probe/enable", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "enabled"


def test_lifecycle_over_rest_requires_plugin_admin(plugin_env: Path, api: dict[str, Any]) -> None:
    admin = api["token_for"]("admin")
    viewer = api["token_for"]("viewer")

    # A viewer may LIST (plugin:read is reachable via *:read) but never install/enable.
    assert api["client"].get("/api/v1/plugins", headers=viewer).status_code == 200
    assert api["client"].post("/api/v1/plugins:discover", headers=viewer).status_code == 403
    assert (
        api["client"].post("/api/v1/plugins/titan.probe/install", headers=viewer).status_code == 403
    )

    _enable(api)
    detail = api["client"].get("/api/v1/plugins/titan.probe", headers=admin).json()
    assert detail["state"] == "enabled"
    assert detail["tools"] == ["probe.echo"]  # contributed via manifest only
    # The operator-granted permission set is recorded for widening detection.
    assert detail["granted_permissions"]["scopes"] == ["task:read"]

    disabled = api["client"].post("/api/v1/plugins/titan.probe/disable", headers=admin)
    assert disabled.status_code == 200 and disabled.json()["state"] == "installed"


def test_ui_panel_serves_with_no_core_changes(plugin_env: Path, api: dict[str, Any]) -> None:
    _enable(api)
    reader = api["token_for"]("viewer")
    resp = api["client"].get("/plugin-ui/titan.probe/panel.html", headers=reader)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Probe panel" in resp.text

    # Path traversal out of the plugin's ui/ directory is refused.
    escape = api["client"].get("/plugin-ui/titan.probe/../titan-plugin.yaml", headers=reader)
    assert escape.status_code == 404

    # Unauthenticated access is refused (a StaticFiles mount would have leaked this).
    assert api["client"].get("/plugin-ui/titan.probe/panel.html").status_code == 401


def test_webhook_requires_a_valid_hmac_signature(plugin_env: Path, api: dict[str, Any]) -> None:
    _enable(api)
    body = json.dumps({"event": "issue.created"}).encode("utf-8")
    signature = hmac.new(_HOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()

    ok = api["client"].post(
        "/api/v1/plugins/titan.probe/hooks/hooks/probe",
        content=body,
        headers={"x-titan-signature": signature, "content-type": "application/json"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["result"]["received"] == {"event": "issue.created"}

    bad = api["client"].post(
        "/api/v1/plugins/titan.probe/hooks/hooks/probe",
        content=body,
        headers={"x-titan-signature": "deadbeef", "content-type": "application/json"},
    )
    assert bad.status_code == 401  # unverified payloads are rejected

    unknown = api["client"].post(
        "/api/v1/plugins/titan.probe/hooks/hooks/nope",
        content=body,
        headers={"x-titan-signature": signature},
    )
    assert unknown.status_code == 404  # no such webhook declared in the manifest
