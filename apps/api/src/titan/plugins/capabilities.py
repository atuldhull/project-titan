"""The capability broker — where the deny-by-default permission classes are checked
(Vol 7 §3.4, ADR-INT-04, TITAN-INT-015).

**Trust boundary (be precise about what this enforces).** Three classes are gated at
the CORE boundary and hold against ANY plugin, cooperative or hostile, because the
resource lives on core's side and can only be reached by crossing back through this
broker: ``secrets`` (only core holds the values), ``scopes`` (only core adjudicates
RBAC), and ``events`` (only core owns the bus). A plugin cannot obtain them without
asking, and this broker denies an unasked-for one.

The two HOST-RESOURCE classes — ``network`` egress and ``filesystem`` reads — are a
different matter. This broker is the *cooperative* path a well-behaved plugin uses,
and it enforces the allowlist/jail on that path; but a hostile plugin in the local
subttprocess regime could ``import urllib`` / ``open()`` directly and bypass it. Real
containment of those two is the PRODUCTION deployment layer per §3.5 — a dedicated
container with a read-only rootfs, non-root UID, and egress restricted to the manifest
allowlist by a sidecar proxy (Kubernetes NetworkPolicy mirrors it). The local
subprocess provides crash/hang isolation (TITAN-INT-016), not egress containment. The
checks below still matter: they gate the cooperative path and are the source of truth
the production sidecar/NetworkPolicy is configured from.

The five classes and where each is checked here:

| class        | granted by                 | checked here                              |
|--------------|----------------------------|-------------------------------------------|
| ``secrets``  | ``permissions.secrets``    | the NAME must be granted; value injected  |
| ``network``  | ``permissions.network``    | the request ORIGIN must match an allowlist pattern |
| ``events``   | ``permissions.events``     | the topic must be in ``publish``          |
| ``filesystem``| ``permissions.filesystem``| workspace-jailed AND glob-matched         |
| ``scopes``   | ``permissions.scopes``     | the RBAC scope must be granted            |

Anything undeclared or ungranted fails closed with ``PERMISSION_DENIED`` and is
audit-logged. Denials are the point of this module — they are never silent.
"""

from collections.abc import Awaitable, Callable
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import structlog

from titan.plugins import jsonrpc
from titan.plugins.jsonrpc import RpcError
from titan.plugins.manifest import Permissions

logger = structlog.get_logger("titan.plugins.capabilities")

#: (plugin_id, capability, detail) -> None. Records a PERMISSION_DENIED audit event.
DenialAuditor = Callable[[str, str, str], Awaitable[None]]
#: name -> secret value (raises KeyError when the store has no such secret).
SecretResolver = Callable[[str], Awaitable[str]]
#: (topic, payload) -> None.
EventPublisher = Callable[[str, dict[str, Any]], Awaitable[None]]
#: (url, method, body) -> response dict.
HttpFetcher = Callable[[str, str, str | None], Awaitable[dict[str, Any]]]


def network_allowed(url: str, patterns: list[str]) -> bool:
    """Match on the request ORIGIN (scheme://host), never the raw string — otherwise
    ``https://evil.test/?x=https://ok.atlassian.net`` would slip a substring match
    (userinfo is dropped by urlparse's ``.hostname``, defeating ``a@evil.test``). A
    pattern may pin a PATH PREFIX, which is enforced at a segment boundary so
    ``.../v1`` does not admit ``.../v1admin``."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    origin = f"{parsed.scheme}://{parsed.hostname}"
    for pattern in patterns:
        pat = urlparse(pattern)
        if not pat.scheme or not pat.hostname:
            continue  # a pattern without a scheme+host is not a usable allowlist entry
        if not fnmatch(origin, f"{pat.scheme}://{pat.hostname}"):
            continue
        prefix = pat.path.rstrip("/")
        # Prefix must align to a path segment: '/v1' admits '/v1' and '/v1/...', not '/v1x'.
        if prefix and parsed.path != prefix and not parsed.path.startswith(prefix + "/"):
            continue
        return True
    return False


def _glob_match(path_parts: tuple[str, ...], pat_parts: tuple[str, ...]) -> bool:
    """Anchored, component-wise glob: ``*`` matches within ONE segment (never crosses
    ``/``) and ``**`` matches any number of segments. So ``reports/*.md`` matches
    ``reports/a.md`` but NOT ``reports/a/b.md`` — directory-scoped grants actually
    scope by directory (unlike a bare ``fnmatch`` whose ``*`` spans separators)."""
    if not pat_parts:
        return not path_parts
    if pat_parts[0] == "**":
        return _glob_match(path_parts, pat_parts[1:]) or (
            bool(path_parts) and _glob_match(path_parts[1:], pat_parts)
        )
    if not path_parts:
        return False
    if fnmatch(path_parts[0], pat_parts[0]):
        return _glob_match(path_parts[1:], pat_parts[1:])
    return False


def filesystem_allowed(rel_path: str, globs: list[str]) -> bool:
    """A workspace-RELATIVE path that escapes the jail (``..``, absolute, Windows
    drive) is refused outright; otherwise it must match a granted glob at segment
    granularity (see :func:`_glob_match`)."""
    pure = PurePosixPath(rel_path.replace("\\", "/"))
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        return False
    return any(_glob_match(pure.parts, PurePosixPath(g).parts) for g in globs)


class CapabilityBroker:
    """Brokers one plugin's ``titan/*`` capability requests against its grant."""

    def __init__(
        self,
        plugin_id: str,
        granted: Permissions,
        *,
        workspace_root: Path | None = None,
        secret_resolver: SecretResolver | None = None,
        event_publisher: EventPublisher | None = None,
        http_fetcher: HttpFetcher | None = None,
        auditor: DenialAuditor | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.granted = granted
        self._workspace_root = workspace_root
        self._secret = secret_resolver
        self._publish = event_publisher
        self._fetch = http_fetcher
        self._audit = auditor
        #: Every denial, for the conformance suite + the operator view.
        self.denials: list[tuple[str, str]] = []

    async def _deny(self, capability: str, detail: str) -> RpcError:
        self.denials.append((capability, detail))
        logger.warning(
            "plugin.permission_denied",
            plugin_id=self.plugin_id,
            capability=capability,
            detail=detail,
        )
        # Best-effort audit: a denial must ALWAYS surface as PERMISSION_DENIED. If the
        # audit write itself fails, we must not let that turn the deny into an
        # INTERNAL_ERROR (which would be a quarantine-counting fault). The structlog
        # line above is the durable-enough local record either way.
        if self._audit is not None:
            try:
                await self._audit(self.plugin_id, capability, detail)
            except Exception:  # noqa: BLE001
                logger.exception("plugin.denial_audit_failed", plugin_id=self.plugin_id)
        return RpcError(jsonrpc.PERMISSION_DENIED, f"PERMISSION_DENIED: {detail}")

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        if method == jsonrpc.C_SECRET_GET:
            return await self._secret_get(params)
        if method == jsonrpc.C_NET_FETCH:
            return await self._net_fetch(params)
        if method == jsonrpc.C_EVENT_PUBLISH:
            return await self._event_publish(params)
        if method == jsonrpc.C_FS_READ:
            return await self._fs_read(params)
        if method == jsonrpc.C_SCOPE_CHECK:
            return await self._scope_check(params)
        raise RpcError(jsonrpc.METHOD_NOT_FOUND, f"unknown capability '{method}'")

    async def _secret_get(self, params: dict[str, Any]) -> str:
        name = str(params.get("name", ""))
        if name not in self.granted.secrets:
            raise await self._deny("secrets", f"secret '{name}' is not granted")
        if self._secret is None:
            raise RpcError(jsonrpc.INTERNAL_ERROR, "no secret store configured")
        try:
            return await self._secret(name)
        except KeyError as exc:
            raise RpcError(jsonrpc.INTERNAL_ERROR, f"secret '{name}' is not set") from exc

    async def _net_fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        url = str(params.get("url", ""))
        if not network_allowed(url, self.granted.network):
            raise await self._deny("network", f"egress to '{url}' is not in the allowlist")
        if self._fetch is None:
            raise RpcError(jsonrpc.INTERNAL_ERROR, "no http fetcher configured")
        return await self._fetch(url, str(params.get("method", "GET")), params.get("body"))

    async def _event_publish(self, params: dict[str, Any]) -> dict[str, Any]:
        topic = str(params.get("topic", ""))
        if topic not in self.granted.events.publish:
            raise await self._deny("events", f"publishing '{topic}' is not granted")
        if self._publish is None:
            raise RpcError(jsonrpc.INTERNAL_ERROR, "no event publisher configured")
        await self._publish(topic, dict(params.get("payload") or {}))
        return {"published": True}

    async def _fs_read(self, params: dict[str, Any]) -> str:
        rel = str(params.get("path", ""))
        if not filesystem_allowed(rel, self.granted.filesystem):
            raise await self._deny("filesystem", f"path '{rel}' is outside the granted globs")
        if self._workspace_root is None:
            raise RpcError(jsonrpc.INTERNAL_ERROR, "no workspace configured")
        target = (self._workspace_root / rel).resolve()
        root = self._workspace_root.resolve()
        # is_relative_to (not str.startswith, which prefix-collides /proj vs /proj-secrets)
        # so a symlink inside the workspace pointing at a sibling dir cannot escape.
        if not target.is_relative_to(root):
            raise await self._deny("filesystem", f"path '{rel}' escapes the workspace")
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RpcError(jsonrpc.INTERNAL_ERROR, f"cannot read '{rel}': {exc}") from exc

    async def _scope_check(self, params: dict[str, Any]) -> dict[str, Any]:
        scope = str(params.get("scope", ""))
        if scope not in self.granted.scopes:
            raise await self._deny("scopes", f"scope '{scope}' is not granted to this plugin")
        return {"granted": True}


__all__ = [
    "CapabilityBroker",
    "DenialAuditor",
    "EventPublisher",
    "HttpFetcher",
    "SecretResolver",
    "filesystem_allowed",
    "network_allowed",
]
