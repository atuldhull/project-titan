"""The plugin manifest (Vol 7 §3.2, TITAN-INT-010). PURE — Pydantic v2 models +
validation with NO I/O, so it is exhaustively fixture-testable and can be published
as JSON Schema (``schemas/plugin-manifest.v1.json``).

Installation must fail with ACTIONABLE, field-level errors on:

* a schema violation (Pydantic reports the offending field path);
* an unknown permission scope (checked against the host's RBAC scope set);
* a ``titan_api`` incompatibility with the running core.

The manifest is the plugin's ONLY declarative surface: tools, agents, UI panels,
webhooks and event subscriptions are all contributed from here, with zero core
changes (TITAN-INT-011).
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: The core plugin-API version this build implements (§3.6 — versioned independently
#: of the product). A manifest's ``titan_api`` range must admit it.
TITAN_API_VERSION = "1.0"

#: Reverse-DNS plugin id, e.g. ``titan.jira``.
_ID_RE = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9][a-z0-9_-]*)+$")
#: SemVer (major.minor.patch with optional pre-release/build).
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
#: One comparator of a titan_api range, e.g. ">=1.0".
_COMPARATOR_RE = re.compile(r"^(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)$")

ToolRisk = Literal["read", "write", "admin"]
PanelType = Literal["iframe", "schema-form", "markdown"]
RuntimeType = Literal["python", "mcp"]


class ManifestError(ValueError):
    """A manifest that cannot be installed. ``errors`` carries one actionable,
    field-level message per problem (TITAN-INT-010)."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


# --- version-range handling (stdlib-only; no packaging dependency) -----------


def _parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """Compare two dotted versions, zero-padding the shorter one."""
    width = max(len(left), len(right))
    a = (*left, *(0,) * (width - len(left)))
    b = (*right, *(0,) * (width - len(right)))
    return (a > b) - (a < b)


def satisfies(version: str, spec: str) -> bool:
    """Does ``version`` satisfy a comma-separated comparator range like
    ``">=1.0,<2.0"``? Every comparator must hold (AND semantics)."""
    target = _parse_version(version)
    for raw in spec.split(","):
        match = _COMPARATOR_RE.match(raw.strip())
        if match is None:
            raise ManifestError([f"titan_api: '{raw.strip()}' is not a valid comparator"])
        op, bound_text = match.groups()
        cmp = _compare(target, _parse_version(bound_text))
        ok = {
            ">=": cmp >= 0,
            "<=": cmp <= 0,
            "==": cmp == 0,
            "!=": cmp != 0,
            ">": cmp > 0,
            "<": cmp < 0,
        }[op]
        if not ok:
            return False
    return True


# --- manifest models ---------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeSpec(_Strict):
    """How the plugin executes. ``python`` runs out-of-process over JSON-RPC/stdio
    (ADR-INT-01); ``mcp`` reuses the same framing (ADR-INT-02) and lands in the next
    phase — the manifest accepts it, the runtime refuses to enable it for now."""

    type: RuntimeType = "python"
    #: ``module:Class`` for a python plugin.
    entrypoint: str | None = Field(default=None, max_length=500)
    python: str | None = Field(default=None, max_length=50)
    # MCP transports (declared here so the schema is stable; Phase 53 implements them).
    transport: Literal["stdio", "http"] | None = None
    command: list[str] = Field(default_factory=list, max_length=50)
    url: str | None = Field(default=None, max_length=2000)


class EventPermissions(_Strict):
    subscribe: list[str] = Field(default_factory=list, max_length=100)
    publish: list[str] = Field(default_factory=list, max_length=100)


class Permissions(_Strict):
    """The five deny-by-default capability classes (§3.4)."""

    #: URL-pattern egress allowlist (e.g. ``https://*.atlassian.net``).
    network: list[str] = Field(default_factory=list, max_length=100)
    #: Named secrets only — values are injected at runtime, never in the manifest.
    secrets: list[str] = Field(default_factory=list, max_length=100)
    events: EventPermissions = Field(default_factory=EventPermissions)
    #: TITAN RBAC scopes the plugin's tools may require.
    scopes: list[str] = Field(default_factory=list, max_length=100)
    #: Workspace-relative path globs.
    filesystem: list[str] = Field(default_factory=list, max_length=100)


class ToolContribution(_Strict):
    name: str = Field(min_length=1, max_length=200)
    schema_path: str | None = Field(default=None, alias="schema", max_length=500)
    #: Inline JSON Schema (an alternative to a schema file reference).
    input_schema: dict[str, Any] = Field(default_factory=dict)
    risk: ToolRisk = "write"  # external schemas are never trusted to self-declare read
    description: str = Field(default="", max_length=2000)
    timeout_s: float | None = Field(default=None, gt=0, le=600)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class UiPanel(_Strict):
    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    #: A named dashboard extension slot (Vol 4), e.g. ``project.sidebar``.
    mount: str = Field(min_length=1, max_length=200)
    type: PanelType = "iframe"
    src: str = Field(default="", max_length=500)


class Webhook(_Strict):
    #: Mounted at /api/v1/plugins/<plugin_id>/hooks/<path>.
    path: str = Field(min_length=1, max_length=200)
    # HMAC-SHA256 only: an unauthenticated ("none") webhook would be an internet-reachable
    # trigger into plugin code (the route is public to the JWT layer), so it is not offered.
    verify: Literal["hmac-sha256"] = "hmac-sha256"
    #: The NAME of the secret holding the shared HMAC key (never the value).
    secret: str | None = Field(default=None, max_length=200)


class Contributes(_Strict):
    tools: list[ToolContribution] = Field(default_factory=list, max_length=200)
    agents: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    ui_panels: list[UiPanel] = Field(default_factory=list, max_length=50)
    webhooks: list[Webhook] = Field(default_factory=list, max_length=50)


class PluginManifest(_Strict):
    """``titan-plugin.yaml`` — schema ``plugin-manifest.v1``."""

    manifest_version: Literal[1] = 1
    id: str = Field(min_length=3, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    author: str = Field(default="", max_length=200)
    license: str = Field(default="", max_length=100)
    homepage: str = Field(default="", max_length=500)
    titan_api: str = Field(default=">=1.0,<2.0", max_length=100)
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    permissions: Permissions = Field(default_factory=Permissions)
    contributes: Contributes = Field(default_factory=Contributes)
    config_schema: str | None = Field(default=None, max_length=500)

    @field_validator("id")
    @classmethod
    def _reverse_dns(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("must be reverse-DNS (e.g. 'titan.jira'): lowercase, dot-separated")
        return value

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not _SEMVER_RE.match(value):
            raise ValueError("must be SemVer (e.g. '1.2.0')")
        return value


def _field_errors(exc: ValidationError) -> list[str]:
    """Pydantic errors → one actionable message per offending field path."""
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        out.append(f"{loc}: {err['msg']}")
    return out


def parse_manifest(
    raw: dict[str, Any],
    *,
    known_scopes: frozenset[str],
    api_version: str = TITAN_API_VERSION,
) -> PluginManifest:
    """Validate a raw manifest into a :class:`PluginManifest`, or raise
    :class:`ManifestError` with a field-level message per problem (TITAN-INT-010).

    Beyond the schema this enforces the two semantic rejections: every requested
    RBAC scope must exist in the host (``known_scopes``), and the ``titan_api``
    range must admit the running core's plugin-API version."""
    try:
        manifest = PluginManifest.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(_field_errors(exc)) from exc

    errors: list[str] = []
    unknown = [s for s in manifest.permissions.scopes if s not in known_scopes]
    for scope in unknown:
        errors.append(f"permissions.scopes: unknown scope '{scope}'")
    if not satisfies(api_version, manifest.titan_api):
        errors.append(
            f"titan_api: core plugin API {api_version} does not satisfy '{manifest.titan_api}'"
        )
    if manifest.runtime.type == "python" and not manifest.runtime.entrypoint:
        errors.append("runtime.entrypoint: required when runtime.type is 'python'")
    # Contributed tool names must be namespaced so a plugin cannot shadow a core tool.
    for tool in manifest.contributes.tools:
        if "." not in tool.name:
            errors.append(
                f"contributes.tools: '{tool.name}' must be namespaced (e.g. 'jira.create_issue')"
            )
    # A webhook that declares HMAC verification must name the secret holding the key.
    for hook in manifest.contributes.webhooks:
        if hook.verify == "hmac-sha256" and not hook.secret:
            errors.append(
                f"contributes.webhooks: '{hook.path}' verify=hmac-sha256 requires a 'secret' name"
            )
        if hook.secret and hook.secret not in manifest.permissions.secrets:
            errors.append(
                f"contributes.webhooks: secret '{hook.secret}' is not in permissions.secrets"
            )
    if errors:
        raise ManifestError(errors)
    return manifest


def json_schema() -> dict[str, Any]:
    """The published JSON Schema for ``plugin-manifest.v1``."""
    schema = PluginManifest.model_json_schema(by_alias=True)
    schema["$id"] = "https://titan.dev/schemas/plugin-manifest.v1.json"
    schema["title"] = "TITAN plugin manifest (v1)"
    return schema


__all__ = [
    "TITAN_API_VERSION",
    "Contributes",
    "EventPermissions",
    "ManifestError",
    "Permissions",
    "PluginManifest",
    "RuntimeSpec",
    "ToolContribution",
    "UiPanel",
    "Webhook",
    "json_schema",
    "parse_manifest",
    "satisfies",
]
