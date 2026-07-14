"""The plugin manifest + its rejection classes (Phase 52; Vol 7 §3.2, TITAN-INT-010).
Pure — no DB, no subprocess. Installation must fail with ACTIONABLE, field-level errors
on a schema violation, an unknown permission scope, or a titan_api incompatibility, and
the error must name the offending field."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from titan.gateway.rbac import KNOWN_SCOPES
from titan.plugins import capabilities
from titan.plugins.manifest import (
    TITAN_API_VERSION,
    ManifestError,
    json_schema,
    parse_manifest,
    satisfies,
)
from titan.plugins.secrets import ConfigCipher, StaticSecretsProvider, verify_hmac_sha256

_GOOD = {
    "manifest_version": 1,
    "id": "titan.sample",
    "name": "Sample",
    "version": "1.0.0",
    "titan_api": ">=1.0,<2.0",
    "runtime": {"type": "python", "entrypoint": "sample_plugin:SamplePlugin"},
    "permissions": {"scopes": ["task:read"], "secrets": ["TOKEN"]},
    "contributes": {"tools": [{"name": "sample.echo", "risk": "read"}]},
}


def test_a_valid_manifest_parses() -> None:
    manifest = parse_manifest(_GOOD, known_scopes=KNOWN_SCOPES)
    assert manifest.id == "titan.sample"
    assert manifest.contributes.tools[0].risk == "read"


# --- the three rejection classes (each names the offending field) ------------


def test_schema_violation_names_the_field() -> None:
    bad = {**_GOOD, "version": "not-semver", "id": "NotReverseDNS"}
    with pytest.raises(ManifestError) as exc:
        parse_manifest(bad, known_scopes=KNOWN_SCOPES)
    joined = " ".join(exc.value.errors)
    assert "version" in joined and "SemVer" in joined
    assert "id" in joined and "reverse-DNS" in joined


def test_unknown_permission_scope_is_rejected() -> None:
    bad = {**_GOOD, "permissions": {"scopes": ["task:read", "nonsense:everything"]}}
    with pytest.raises(ManifestError) as exc:
        parse_manifest(bad, known_scopes=KNOWN_SCOPES)
    assert any("permissions.scopes" in e and "nonsense:everything" in e for e in exc.value.errors)


def test_titan_api_incompatibility_is_rejected() -> None:
    bad = {**_GOOD, "titan_api": ">=2.0"}
    with pytest.raises(ManifestError) as exc:
        parse_manifest(bad, known_scopes=KNOWN_SCOPES)
    assert any("titan_api" in e for e in exc.value.errors)
    assert satisfies(TITAN_API_VERSION, ">=1.0,<2.0")
    assert not satisfies(TITAN_API_VERSION, ">=2.0")


def test_unnamespaced_tool_and_unbacked_webhook_secret_are_rejected() -> None:
    bad = {
        **_GOOD,
        "contributes": {
            "tools": [{"name": "echo"}],  # not namespaced → could shadow a core tool
            "webhooks": [{"path": "/hooks/x", "verify": "hmac-sha256", "secret": "NOT_DECLARED"}],
        },
    }
    with pytest.raises(ManifestError) as exc:
        parse_manifest(bad, known_scopes=KNOWN_SCOPES)
    joined = " ".join(exc.value.errors)
    assert "namespaced" in joined
    assert "not in permissions.secrets" in joined


def test_python_runtime_requires_an_entrypoint() -> None:
    bad = {**_GOOD, "runtime": {"type": "python"}}
    with pytest.raises(ManifestError, match="entrypoint"):
        parse_manifest(bad, known_scopes=KNOWN_SCOPES)


# --- MCP runtime validation (Phase 53; Vol 7 §7, TITAN-INT-050) --------------

_MCP_STDIO = {
    "manifest_version": 1,
    "id": "titan.mcp_fs",
    "name": "MCP FS",
    "version": "1.0.0",
    "titan_api": ">=1.0,<2.0",
    "runtime": {
        "type": "mcp",
        "transport": "stdio",
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."],
    },
    "permissions": {"scopes": ["workspace:read"]},
}

_MCP_HTTP = {
    "manifest_version": 1,
    "id": "titan.mcp_remote",
    "name": "MCP Remote",
    "version": "1.0.0",
    "titan_api": ">=1.0,<2.0",
    "runtime": {
        "type": "mcp",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "auth_secret": "MCP_TOKEN",
    },
    "permissions": {
        "scopes": ["workspace:read"],
        "secrets": ["MCP_TOKEN"],
        "network": ["https://mcp.example.com"],
    },
}


def test_valid_mcp_stdio_and_http_manifests_parse() -> None:
    stdio = parse_manifest(_MCP_STDIO, known_scopes=KNOWN_SCOPES)
    assert stdio.runtime.type == "mcp" and stdio.runtime.transport == "stdio"
    assert stdio.runtime.command[0] == "npx"
    http = parse_manifest(_MCP_HTTP, known_scopes=KNOWN_SCOPES)
    assert http.runtime.transport == "http" and http.runtime.url == "https://mcp.example.com/mcp"
    assert http.runtime.auth_secret == "MCP_TOKEN"


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        ({"transport": None}, "transport 'stdio' or 'http'"),
        ({"transport": "stdio", "command": []}, "command"),
        ({"transport": "stdio", "url": "https://x.test"}, "not valid for an MCP stdio"),
        ({"transport": "stdio", "entrypoint": "mod:Cls"}, "not valid for an MCP runtime"),
    ],
)
def test_mcp_stdio_shape_is_enforced(mutation: dict[str, object], needle: str) -> None:
    runtime = {"type": "mcp", "transport": "stdio", "command": ["srv"], **mutation}
    bad = {**_MCP_STDIO, "runtime": runtime}
    with pytest.raises(ManifestError) as exc:
        parse_manifest(bad, known_scopes=KNOWN_SCOPES)
    assert any(needle in e for e in exc.value.errors), exc.value.errors


def test_mcp_http_requires_a_url_and_backed_auth_secret() -> None:
    no_url = {**_MCP_HTTP, "runtime": {"type": "mcp", "transport": "http"}}
    with pytest.raises(ManifestError) as exc:
        parse_manifest(no_url, known_scopes=KNOWN_SCOPES)
    assert any("url" in e for e in exc.value.errors)

    # auth_secret must be declared in permissions.secrets (never inlined).
    unbacked = {
        **_MCP_HTTP,
        "permissions": {"scopes": ["workspace:read"], "network": ["https://mcp.example.com"]},
    }
    with pytest.raises(ManifestError) as exc:
        parse_manifest(unbacked, known_scopes=KNOWN_SCOPES)
    assert any("auth_secret" in e and "permissions.secrets" in e for e in exc.value.errors)


def test_reserved_mcp_prefix_on_a_python_tool_is_rejected() -> None:
    """A python plugin may not contribute a tool under the ``mcp.`` prefix — that namespace
    belongs to MCP servers, and impersonating one would confuse the registry/introspection."""
    bad = {**_GOOD, "contributes": {"tools": [{"name": "mcp.trusted.tool"}]}}
    with pytest.raises(ManifestError) as exc:
        parse_manifest(bad, known_scopes=KNOWN_SCOPES)
    assert any("reserved 'mcp.' prefix" in e for e in exc.value.errors)


def test_committed_filesystem_mcp_manifest_validates() -> None:
    """The reference server-filesystem plugin ships as a manifest ONLY (zero TITAN code);
    it must validate against the running core so it can be discovered + installed (DoD)."""
    import yaml

    manifest_path = (
        Path(__file__).parents[3]
        / "plugins"
        / "first_party"
        / "mcp_filesystem"
        / "titan-plugin.yaml"
    )
    assert manifest_path.exists()
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest = parse_manifest(raw, known_scopes=KNOWN_SCOPES)
    assert manifest.id == "titan.mcp_filesystem"
    assert manifest.runtime.type == "mcp" and manifest.runtime.transport == "stdio"


# --- the published JSON Schema (drift-gated artifact) ------------------------


def test_published_json_schema_is_current() -> None:
    """schemas/plugin-manifest.v1.json is generated from the Pydantic model; the
    committed copy must match byte-for-byte (the same drift discipline as openapi.json)."""
    committed = Path(__file__).parents[3] / "packages" / "schemas" / "plugin-manifest.v1.json"
    assert committed.exists(), "run scripts/export_plugin_schema.py"
    expected = json.dumps(json_schema(), indent=2, sort_keys=True) + "\n"
    assert committed.read_text(encoding="utf-8") == expected


# --- secrets: config encryption + webhook HMAC ------------------------------


def test_config_round_trips_encrypted_with_a_key_version_tag() -> None:
    cipher = ConfigCipher("a-test-key")
    blob = cipher.encrypt({"board": "ENG", "limit": 5})
    assert blob.startswith("v1.")  # key-version tagged (SEC-063)
    assert "ENG" not in blob  # actually encrypted, not encoded
    assert cipher.decrypt(blob) == {"board": "ENG", "limit": 5}
    assert ConfigCipher("").decrypt("") == {}


def test_config_decrypt_with_the_wrong_key_fails_loudly() -> None:
    blob = ConfigCipher("key-a").encrypt({"x": 1})
    with pytest.raises(ValueError, match="could not be decrypted"):
        ConfigCipher("key-b").decrypt(blob)


def test_webhook_hmac_verification() -> None:
    body = b'{"event":"issue.created"}'
    import hashlib
    import hmac as _hmac

    good = _hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_hmac_sha256("s3cret", body, good)
    assert verify_hmac_sha256("s3cret", body, f"sha256={good}")  # prefixed form
    assert not verify_hmac_sha256("s3cret", body, "deadbeef")
    assert not verify_hmac_sha256("", body, good)  # no secret → never verifies


# --- capability matchers (the network/filesystem guards) --------------------


def test_network_allowlist_matches_origin_not_substring() -> None:
    allow = ["https://*.atlassian.net"]
    assert capabilities.network_allowed("https://api.atlassian.net/rest/x", allow)
    assert not capabilities.network_allowed("https://evil.test/?x=https://api.atlassian.net", allow)
    assert not capabilities.network_allowed("http://api.atlassian.net", allow)  # scheme pinned
    assert not capabilities.network_allowed("https://atlassian.net.evil.test", allow)


def test_filesystem_globs_refuse_traversal_and_scope_by_directory() -> None:
    allow = ["reports/*.md"]
    assert capabilities.filesystem_allowed("reports/a.md", allow)
    assert not capabilities.filesystem_allowed("reports/../../etc/passwd", allow)
    assert not capabilities.filesystem_allowed("/etc/passwd", allow)
    assert not capabilities.filesystem_allowed("secrets/a.md", allow)
    # '*' must NOT cross '/': a directory-scoped grant scopes by directory.
    assert not capabilities.filesystem_allowed("reports/sub/a.md", allow)
    assert not capabilities.filesystem_allowed("a.md", allow)
    # '**' matches any depth when explicitly granted.
    assert capabilities.filesystem_allowed("reports/sub/a.md", ["reports/**/*.md"])


def test_network_path_prefix_is_segment_bounded() -> None:
    allow = ["https://api.test/v1"]
    assert capabilities.network_allowed("https://api.test/v1", allow)
    assert capabilities.network_allowed("https://api.test/v1/issues", allow)
    assert not capabilities.network_allowed("https://api.test/v1admin", allow)  # boundary


async def _secret(name: str) -> str:
    return await StaticSecretsProvider({"TOKEN": "v"}).get(name)


def test_static_secret_provider_raises_keyerror_for_unknown() -> None:
    import asyncio

    assert asyncio.run(_secret("TOKEN")) == "v"
    with pytest.raises(KeyError):
        asyncio.run(_secret("MISSING"))


def test_host_module_is_stdlib_only() -> None:
    """The plugin-side host runs in the plugin's own venv (which has no titan, no
    pydantic), so it must import nothing outside the stdlib."""
    host = Path(__file__).parents[1] / "src" / "titan" / "plugins" / "host.py"
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-I", "-c", f"import ast,sys; ast.parse(open(r'{host}').read())"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    source = host.read_text(encoding="utf-8")
    for banned in ("import pydantic", "import sqlalchemy", "from titan", "import titan"):
        assert banned not in source, f"host.py must not {banned}"
