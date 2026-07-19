"""Plugin Manager read models + MCP acquisition (Vol 4 §6.12, Phase 64).

Private to the `plugins` package; `router.py` is the only importer.

Phase 52/53 built the whole lifecycle — discover, install, enable, disable,
upgrade, remove, configure — but built it for an operator with shell access. Four
things a *screen* needs did not exist, and this module adds exactly those:

1. **The config schema, as data.** ``manifest.config_schema`` is a *path string*
   pointing at a JSON Schema inside the plugin's own directory, and nothing ever
   read it. TITAN-FRT-035 requires the config form be generated from that schema,
   so it has to be fetchable over HTTP.
2. **A readable config.** ``PluginInstallOut`` has no ``config`` field, so the
   saved config never round-trips — which satisfies "a secret never appears in
   an API response" trivially, but also leaves a form unable to show what is
   already set. This adds a REDACTED read: ordinary values verbatim, and every
   field the schema marks ``writeOnly`` replaced by a sentinel.
3. **Secret status.** ``permissions.secrets`` names secrets that TITAN never
   stores — :class:`EnvSecretsProvider` resolves them from the process
   environment (Vol 8 SEC-050). So the UI cannot offer to *save* one; the honest
   affordance is to say which are resolvable and which are missing.
4. **MCP add-by-URL/command.** Phase 53 can only reach an MCP server that is
   already a directory on disk with a ``titan-plugin.yaml``. §6.12's "add by
   URL/command" plus "tool-list preview" needs both a writer for that directory
   and a way to connect *before* committing to an install.
"""

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field

from titan.plugins.manifest import PluginManifest, parse_manifest
from titan.plugins.secrets import SecretsProvider
from titan.plugins.service import MANIFEST_FILENAME, PluginInstallOut

logger = structlog.get_logger("titan.plugins.manager")

#: What a write-only field reads as once saved. The value itself is never sent.
SECRET_SENTINEL = "••••"

#: Plugin ids are reverse-DNS-ish; this is also the directory name, so it is
#: validated before it is ever joined to a path.
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,199}$")


class PluginConfigOut(BaseModel):
    """What the config form needs, in one response."""

    plugin_id: str
    #: The plugin's JSON Schema, verbatim, or None when it declares no config.
    config_schema: dict[str, Any] | None = None
    #: True when the manifest DECLARES a config_schema that could not be read
    #: (missing, unparseable, or outside the plugin directory). Distinct from
    #: "declares none": the schema is what identifies secrets, so without it
    #: every value is masked and the form is read-only rather than fail-open.
    schema_unavailable: bool = False
    #: Saved values with every write-only field replaced by the sentinel.
    values: dict[str, Any] = Field(default_factory=dict)
    #: Keys the schema marks write-only — the UI renders these as password
    #: inputs and never submits the sentinel back as a value.
    secret_keys: list[str] = Field(default_factory=list)
    #: Manifest-declared secrets (env-resolved, never stored by TITAN).
    secrets: list["SecretStatus"] = Field(default_factory=list)


class SecretStatus(BaseModel):
    """One entry of ``permissions.secrets``.

    ``resolved`` is whether the runtime can currently get a value for it. There
    is deliberately no way to SET one here: Vol 8 SEC-050 keeps secret material
    out of application tables entirely, so the value lives in the environment
    (``TITAN_PLUGIN_SECRET_<NAME>``) and an operator sets it there. A UI field
    that accepted one would have nowhere honest to put it.
    """

    name: str
    resolved: bool
    env_var: str


class PluginHealthOut(BaseModel):
    plugin_id: str
    state: str
    #: True when the supervisor holds a live worker/connection for this plugin.
    running: bool
    #: Tools this plugin currently contributes to the registry.
    tools: list[str] = Field(default_factory=list)
    error_count: int = 0
    last_error: str = ""


class McpServerSpec(BaseModel):
    """§6.12's "add by URL/command". Mirrors ``RuntimeSpec`` for MCP, but is a
    separate DTO because the manifest allows shapes this endpoint must not (a
    python entrypoint, for instance)."""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(min_length=3, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    transport: str = Field(pattern="^(stdio|http)$")
    #: stdio only — argv of the server process.
    command: list[str] = Field(default_factory=list, max_length=50)
    #: http only — the Streamable HTTP endpoint.
    url: str | None = Field(default=None, max_length=2000)
    #: Name of the secret holding a bearer token, if the server needs one.
    auth_secret: str | None = Field(default=None, max_length=200)
    #: RBAC scopes the server's tools will require once enabled.
    scopes: list[str] = Field(default_factory=list, max_length=100)
    #: Hosts the plugin may reach; an http server's own host must be allowed.
    network: list[str] = Field(default_factory=list, max_length=100)


class McpToolPreview(BaseModel):
    name: str
    description: str = ""


class McpPreviewOut(BaseModel):
    """The result of connecting to a candidate server WITHOUT installing it."""

    ok: bool
    protocol_version: str = ""
    server_info: dict[str, Any] = Field(default_factory=dict)
    tools: list[McpToolPreview] = Field(default_factory=list)
    detail: str = ""


PluginConfigOut.model_rebuild()


# --- config schema + redaction ----------------------------------------------


def _schema_path(install: PluginInstallOut) -> Path | None:
    """Resolve ``config_schema`` inside the plugin's own directory.

    The manifest is plugin-authored, so the path is untrusted input: it is
    resolved and then required to stay under the plugin directory, the same
    containment the UI-asset route applies. A manifest naming
    ``../../../etc/passwd`` gets None, not a file read.
    """
    declared = str((install.manifest or {}).get("config_schema") or "").strip()
    if not declared or not install.plugin_dir:
        return None
    root = Path(install.plugin_dir).resolve()
    target = (root / declared).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        logger.warning(
            "plugin.config_schema_rejected", plugin_id=install.plugin_id, declared=declared
        )
        return None
    return target


def load_config_schema(install: PluginInstallOut) -> dict[str, Any] | None:
    """Read the plugin's JSON Schema. JSON or YAML — ``yaml.safe_load`` parses
    both, and JSON Schema files are commonly written as either."""
    path = _schema_path(install)
    if path is None:
        return None
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "plugin.config_schema_unreadable", plugin_id=install.plugin_id, error=str(exc)
        )
        return None
    return dict(loaded) if isinstance(loaded, dict) else None


def secret_keys_of(schema: dict[str, Any] | None) -> list[str]:
    """Top-level property names the schema marks as secret.

    ``writeOnly`` is JSON Schema's own vocabulary for exactly this (draft
    2019-09 §9.4: "the value is never present in a response"), so the manifest
    author declares it in the standard way rather than through a TITAN-specific
    convention. ``format: password`` is accepted too — it is what most schema
    authors reach for first.
    """
    if not schema:
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    keys: list[str] = []
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        if bool(spec.get("writeOnly")) or spec.get("format") == "password":
            keys.append(str(name))
    return sorted(keys)


def declares_config_schema(install: PluginInstallOut) -> bool:
    """True when the manifest NAMES a config schema, whether or not it loads."""
    return bool(str((install.manifest or {}).get("config_schema") or "").strip())


def redact_config(
    config: dict[str, Any], secret_keys: list[str], *, mask_all: bool = False
) -> dict[str, Any]:
    """Replace every saved secret value with the sentinel.

    A key that is absent stays absent: rendering ``••••`` for a field that was
    never set would tell the operator a secret exists where none does, and they
    would then leave it alone believing it configured.

    ``mask_all`` is the FAIL-CLOSED path. The schema is the only thing that
    identifies which keys are secret, so when a plugin declares one that cannot
    be read — a syntax error, a rename, a path that fails containment — an
    empty secret-key list would mean "nothing here is secret" and every stored
    credential would be returned verbatim. Masking everything is the safe
    reading of "we do not know which of these are secrets".
    """
    secret = set(secret_keys)
    return {
        key: (SECRET_SENTINEL if mask_all or key in secret else value)
        for key, value in config.items()
    }


def strip_sentinels(
    submitted: dict[str, Any], current: dict[str, Any], secret_keys: list[str]
) -> dict[str, Any]:
    """Fold a submitted config OVER the saved one, keeping untouched secrets.

    Two distinct preservations, and both are load-bearing:

    * **The mask.** The form renders ``••••`` for a saved secret, so an
      unchanged form submits the sentinel back. Storing it verbatim would
      overwrite the real credential with four bullet characters — the config
      would look correct and the plugin would start failing to authenticate.
      A secret field still equal to the sentinel means "leave as it is".
    * **Keys the client never saw.** This is a genuine fold, starting from
      ``current``, because ``configure`` REPLACES the stored document. Anything
      the submitting form could not render would otherwise be deleted — a
      schema type the form engine does not support, a key left over from an
      earlier plugin version, and in particular ``risk_overrides``, which the
      supervisor reads (per-tool MCP risk downgrades) and which NO config
      schema declares, so no form will ever submit it.

    A caller that genuinely wants to clear a key must therefore do so
    explicitly; it cannot happen as a side effect of a field the UI could not
    draw.
    """
    secret = set(secret_keys)
    merged = {**current, **submitted}
    for key in secret:
        if merged.get(key) == SECRET_SENTINEL:
            if key in current:
                merged[key] = current[key]
            else:
                merged.pop(key, None)
    return merged


async def secret_status(
    manifest: dict[str, Any], secrets: SecretsProvider, *, env_prefix: str
) -> list[SecretStatus]:
    declared = ((manifest or {}).get("permissions") or {}).get("secrets") or []
    out: list[SecretStatus] = []
    for raw in declared:
        name = str(raw)
        try:
            value = await secrets.get(name)
            resolved = bool(value)
        except KeyError:
            resolved = False
        out.append(SecretStatus(name=name, resolved=resolved, env_var=f"{env_prefix}{name}"))
    return out


# --- MCP acquisition ---------------------------------------------------------


def _origin_of(url: str) -> str:
    """``https://host:port`` from a URL, or "" when it does not parse as one.

    An unparseable URL is left for ``parse_manifest`` to reject, which produces
    a far better message than anything this could raise.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def manifest_document(spec: McpServerSpec) -> dict[str, Any]:
    """The ``titan-plugin.yaml`` document for an added MCP server."""
    runtime: dict[str, Any] = {"type": "mcp", "transport": spec.transport}
    if spec.transport == "stdio":
        runtime["command"] = list(spec.command)
    else:
        runtime["url"] = spec.url
    if spec.auth_secret:
        runtime["auth_secret"] = spec.auth_secret

    permissions: dict[str, Any] = {}
    if spec.scopes:
        permissions["scopes"] = list(spec.scopes)
    # An http server is unreachable without an egress allowance for its OWN
    # origin, and auth_secret must appear in permissions.secrets or the manifest
    # validator rejects it. Both are DERIVED here rather than left as a trap the
    # operator only discovers at enable time — and deriving the origin server-
    # side matters because the client's copy of this rule is advisory: a request
    # that omits `network` would otherwise produce a server nothing can reach.
    network = list(spec.network)
    if spec.transport == "http" and spec.url:
        origin = _origin_of(spec.url)
        if origin and origin not in network:
            network.append(origin)
    if network:
        permissions["network"] = network
    if spec.auth_secret:
        permissions["secrets"] = [spec.auth_secret]

    document: dict[str, Any] = {
        "manifest_version": 1,
        "id": spec.plugin_id,
        "name": spec.name,
        "version": "0.1.0",
        "runtime": runtime,
    }
    if spec.description:
        document["description"] = spec.description
    if permissions:
        document["permissions"] = permissions
    return document


def validate_spec(spec: McpServerSpec, *, known_scopes: frozenset[str]) -> PluginManifest:
    """Build and validate the manifest this spec would produce.

    Runs the REAL ``parse_manifest``, so an add request is rejected by exactly
    the rules an install would apply — an unknown scope, a stdio server with no
    command, an http server with no url — instead of writing a directory that
    then fails to discover.
    """
    if not _SAFE_ID.match(spec.plugin_id):
        raise ValueError(
            "plugin_id must be lowercase alphanumeric with . _ - separators, 3-200 chars"
        )
    return parse_manifest(manifest_document(spec), known_scopes=known_scopes)


def write_mcp_manifest(plugins_dir: Path, spec: McpServerSpec) -> Path:
    """Materialise the server as a plugin directory the Phase-52 scanner finds.

    This is deliberately the same on-disk shape a first-party plugin has, so an
    added MCP server is not a second class of thing with its own lifecycle: it
    installs, enables, quarantines and upgrades through the code that already
    exists.
    """
    target = plugins_dir / spec.plugin_id
    if target.exists():
        raise FileExistsError(f"a plugin directory named '{spec.plugin_id}' already exists")
    target.mkdir(parents=True)
    (target / MANIFEST_FILENAME).write_text(
        "# Generated by TITAN Settings → Plugins → MCP Servers.\n"
        + yaml.safe_dump(manifest_document(spec), sort_keys=False),
        encoding="utf-8",
    )
    return target


def tool_previews(tools: list[Any]) -> list[McpToolPreview]:
    return [
        McpToolPreview(
            name=str(getattr(t, "name", "")),
            description=str(getattr(t, "description", "") or "")[:500],
        )
        for t in tools
    ]


def dumps_schema(schema: dict[str, Any]) -> str:
    """Stable JSON for a schema, used when echoing one into a test fixture."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


__all__ = [
    "SECRET_SENTINEL",
    "McpPreviewOut",
    "McpServerSpec",
    "McpToolPreview",
    "PluginConfigOut",
    "PluginHealthOut",
    "SecretStatus",
    "dumps_schema",
    "load_config_schema",
    "manifest_document",
    "redact_config",
    "secret_keys_of",
    "secret_status",
    "strip_sentinels",
    "tool_previews",
    "validate_spec",
    "write_mcp_manifest",
]
