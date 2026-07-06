"""Reusable TITAN-SEC-073 parameter constraints for tool arguments.

LLM tool-call arguments are schema-validated AND constraint-checked before
execution: paths canonicalized and confined to ``/workspace``, shell arguments
passed as argv arrays (never ``sh -c``), URLs checked against allowlists with
private/link-local/metadata ranges blocked. These factories return
:data:`~titan.tools.registry.ParamConstraint` callables the Tool Bus runs in
step 2; the fs.*/terminal.*/web.* tools of Phases 20–21 attach them.

Rejections use the taxonomy: a jailbreak path or metadata URL is a
``SANDBOX_VIOLATION``; a malformed argument shape is ``SCHEMA_INVALID``.
"""

import ipaddress
import posixpath
from typing import Any
from urllib.parse import urlsplit

from titan.tools.errors import ToolErrorCode
from titan.tools.registry import ParamConstraint

_WORKSPACE = "/workspace"
# Metacharacters that only matter when a string is handed to a shell; argv
# execution neutralizes them, so their presence signals an argument meant to
# break out of argv into `sh -c`.
_SHELL_METACHARS = frozenset(";&|`$><\n\\")

Violation = tuple[ToolErrorCode, str]


def _canonical_workspace_path(raw: str) -> str | None:
    """The absolute, symlink-naive canonical form of ``raw`` if it stays inside
    ``/workspace``, else None. Rejects absolute escapes, ``..`` traversal, and
    NUL injection without touching the filesystem (canonicalization is a string
    operation so it is safe to run before the sandbox exists)."""
    if not raw or "\x00" in raw or "\\" in raw:
        # Backslashes never appear in a legitimate POSIX workspace path; they
        # are a Windows-style traversal attempt (..\..\) — reject outright.
        return None
    # Relative paths resolve against the workspace root; absolute paths must
    # already be under it.
    candidate = raw if posixpath.isabs(raw) else posixpath.join(_WORKSPACE, raw)
    normalized = posixpath.normpath(candidate)
    if normalized != _WORKSPACE and not normalized.startswith(_WORKSPACE + "/"):
        return None
    return normalized


def workspace_path(*fields: str) -> ParamConstraint:
    """Confine each named path argument to ``/workspace`` (TITAN-SEC-073).
    Path-escape attempts (``../../etc/passwd``, absolute ``/etc/shadow``) are
    SANDBOX_VIOLATIONs, not literal filenames."""

    def check(arguments: dict[str, Any]) -> Violation | None:
        for field in fields:
            value = arguments.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                return ("SCHEMA_INVALID", f"at '/{field}': path must be a string")
            if _canonical_workspace_path(value) is None:
                return (
                    "SANDBOX_VIOLATION",
                    f"at '/{field}': path escapes the /workspace sandbox",
                )
        return None

    return check


def argv_only(*fields: str) -> ParamConstraint:
    """Require each named command argument to be an argv ARRAY of strings, and
    reject shell metacharacters (TITAN-SEC-073: never ``sh -c``). A
    ``"; rm -rf /"`` payload is a SANDBOX_VIOLATION."""

    def check(arguments: dict[str, Any]) -> Violation | None:
        for field in fields:
            value = arguments.get(field)
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return (
                    "SCHEMA_INVALID",
                    f"at '/{field}': command must be an argv array of strings, not a shell string",
                )
            for item in value:
                if _SHELL_METACHARS & set(item):
                    return (
                        "SANDBOX_VIOLATION",
                        f"at '/{field}': shell metacharacters are not permitted in argv",
                    )
        return None

    return check


def _parse_ipv4_part(part: str) -> int:
    """Parse one IPv4 octet the way ``inet_aton`` does: ``0x``/``0X`` prefix is
    hex, a leading ``0`` (with more digits) is octal, otherwise decimal.
    Raises ValueError on anything that is not such a literal."""
    lowered = part.lower()
    if lowered.startswith("0x"):
        return int(lowered, 16)
    if lowered.startswith("0") and len(lowered) > 1:
        return int(lowered, 8)
    return int(lowered, 10)


def _coerce_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse the alternate-radix / short IPv4 forms ``getaddrinfo``/``inet_aton``
    accept but ``ipaddress`` rejects — decimal (``2130706433``), hex
    (``0x7f000001``), octal (``0177.0.0.1``), and 1–3 part addresses. Returns
    None for anything that is not such a literal (i.e. a real DNS name)."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or any(part == "" for part in parts):
        return None
    try:
        nums = [_parse_ipv4_part(part) for part in parts]
    except ValueError:
        return None
    if any(num < 0 for num in nums):
        return None
    if len(nums) == 1:
        value = nums[0]
    else:
        # inet_aton: leading parts are single bytes; the last absorbs the rest.
        if any(num > 0xFF for num in nums[:-1]):
            return None
        tail_max = 0xFFFFFFFF >> (8 * (len(nums) - 1))
        if nums[-1] > tail_max:
            return None
        value = nums[-1]
        for i, num in enumerate(nums[:-1]):
            value |= num << (8 * (3 - i))
    if value > 0xFFFFFFFF:
        return None
    return ipaddress.IPv4Address(value)


def _host_is_blocked(host: str) -> bool:
    """True if ``host`` is (or resolves literally to) a private, loopback,
    link-local, or cloud-metadata address. DNS-name SSRF re-checks after
    resolution belong to the egress proxy (TITAN-SEC-074); here we block the
    literal-IP (in every encoding a resolver honors) and metadata-name cases the
    corpus fires."""
    # Strip the DNS root-label trailing dot ('localhost.' resolves like
    # 'localhost') so it can't slip past the name/suffix checks below.
    host = host.rstrip(".").lower()
    if host in {"metadata.google.internal", "metadata", "localhost"} or host.endswith(
        (".localhost", ".internal", ".local")
    ):
        return True
    try:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        ip = _coerce_ipv4(host)  # decimal/hex/octal/short IPv4 → still an IP
    if ip is None:
        return False  # a genuine DNS name — deferred to the egress proxy
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 covers the 169.254.169.254 metadata IP
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def host_is_blocked(host: str) -> bool:
    """Public alias of the private-range/metadata host check, reused by the
    browser.* SSRF guard (TITAN-SEC-073/074)."""
    return _host_is_blocked(host)


def url_allowlist(field: str, *, allowed_hosts: frozenset[str] | None = None) -> ParamConstraint:
    """Require the named URL argument to be http(s), optionally host-allowlisted,
    with private/link-local/metadata targets blocked (TITAN-SEC-073). An
    ``http://169.254.169.254/…`` payload is a SANDBOX_VIOLATION."""

    def check(arguments: dict[str, Any]) -> Violation | None:
        value = arguments.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            return ("SCHEMA_INVALID", f"at '/{field}': url must be a string")
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"}:
            return ("SANDBOX_VIOLATION", f"at '/{field}': only http(s) URLs are permitted")
        host = parts.hostname
        if not host:
            return ("SCHEMA_INVALID", f"at '/{field}': url has no host")
        host = host.rstrip(".").lower()  # normalize once for both checks below
        if _host_is_blocked(host):
            return (
                "SANDBOX_VIOLATION",
                f"at '/{field}': url targets a private/link-local/metadata address",
            )
        if allowed_hosts is not None and host not in allowed_hosts:
            return ("SANDBOX_VIOLATION", f"at '/{field}': host {host!r} is not on the allowlist")
        return None

    return check


__all__ = ["argv_only", "host_is_blocked", "url_allowlist", "workspace_path"]
