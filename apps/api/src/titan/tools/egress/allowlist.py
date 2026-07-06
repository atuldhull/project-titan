"""Host allowlist for the forward proxy (Phase 23; TITAN-SEC-043).

From inside the sandbox only the package-registry hosts and the task's git
remote are reachable (plus any hosts an Admin has opted the project into via the
SEC-033 approval flow — carried in the per-task token). Everything else is
denied. This is a pure, offline decision: the token already carries the git
remote + approved hosts, and the registry set is a constant, so the hot path
needs no DB round-trip."""

from titan.tools.egress.tokens import TokenClaims

# The package indexes pip/npm/yarn reach. The sandbox is pointed at the caching
# gateway mirror for these, but the real hosts are allowlisted too so an install
# works whether it targets the mirror or (as a fallback) the index directly — and
# either way every request traverses THIS audited, attributed proxy.
REGISTRY_HOSTS: frozenset[str] = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "pypi.python.org",
        "registry.npmjs.org",
        "registry.yarnpkg.com",
    }
)


class Allowlist:
    """Decides whether a proxied host is reachable for a given task identity."""

    def __init__(self, *, mirror_hosts: frozenset[str] = frozenset()) -> None:
        # The gateway host(s) serving /pypi + /npm (the caching mirror the sandbox
        # is pointed at). Configured; empty in the pure allowlist-only mode.
        self._mirror_hosts = frozenset(h.lower() for h in mirror_hosts)

    def is_allowed(self, host: str, claims: TokenClaims) -> bool:
        host = host.rstrip(".").lower()
        if host in REGISTRY_HOSTS or host in self._mirror_hosts:
            return True
        if claims.git_host and host == claims.git_host.rstrip(".").lower():
            return True
        return host in {h.rstrip(".").lower() for h in claims.approved_hosts}


__all__ = ["REGISTRY_HOSTS", "Allowlist"]
