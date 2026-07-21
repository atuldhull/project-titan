"""Resolve the real client IP behind the BFF (TITAN-SEC-017).

**Why this module has to exist before per-IP throttling does.** Every browser
request reaches FastAPI through the Next.js BFF, so `request.client.host` is
the BFF's address for every user in the deployment. Per-IP backoff keyed on
that value is not per-IP at all: five bad logins from one attacker would lock
out every browser user at once, and a test written against it would pass while
proving nothing.

Until Phase 67 nothing in the codebase looked at a forwarding header at all —
no `X-Forwarded-For` handling, no trusted-proxy setting, and uvicorn's own
`ProxyHeaders` middleware is not enabled (its default trust list of
``127.0.0.1`` would not match the web container's bridge address anyway).

**Why the header is not simply trusted.** `X-Forwarded-For` is client-supplied.
Trusting it unconditionally hands every attacker a per-IP throttle bypass —
one header per request and each attempt looks like a new address — and lets
them forge someone else's address to get *that* user throttled instead. So the
header is honoured only when the immediate peer is a configured trusted proxy,
and otherwise ignored in favour of the socket address.

The list is rightmost-first by trust: the last entry is appended by the nearest
proxy and is the only one that proxy vouches for. We walk right-to-left across
entries while each hop is itself trusted, and take the first address that is
not — that is the earliest address the trust chain can actually vouch for.
Walking left-to-right (taking `xff[0]`) is the classic mistake: the leftmost
entry is whatever the original client sent, which is to say whatever the
attacker typed.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Protocol

# ipaddress._BaseNetwork is private and unparameterised; mypy --strict rejects
# it as a bare generic. The public union is the same thing with a name.
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.datastructures import Address, Headers


class _RequestLike(Protocol):
    """The subset of ``starlette.Request`` this resolver reads. Typing to it
    (rather than the full ``Request``, which needs an ASGI scope to construct)
    lets a per-IP test drive the resolver directly with a peer/headers stub —
    the whole point, since TestClient fixes the peer address."""

    @property
    def client(self) -> Address | None: ...

    @property
    def headers(self) -> Headers: ...


# The address recorded when there is no peer at all. Starlette leaves
# `request.client` as None for ASGI transports that carry no client scope —
# notably TestClient's default and lifespan-only calls.
UNKNOWN_IP = "unknown"


def _networks(cidrs: Sequence[str]) -> list[IPNetwork]:
    parsed: list[IPNetwork] = []
    for cidr in cidrs:
        candidate = cidr.strip()
        if not candidate:
            continue
        try:
            # `strict=False` so a bare host address ("10.0.0.7") is accepted as
            # the /32 it obviously means, rather than raising.
            parsed.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            # A malformed entry must not be silently treated as "trust
            # nothing" OR "trust everything" — skipping it is the only safe
            # reading, and the setting validator rejects these at startup so
            # this branch is a belt-and-braces guard.
            continue
    return parsed


def _is_trusted(address: str, networks: Sequence[IPNetwork]) -> bool:
    if not networks:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def resolve_client_ip(request: _RequestLike, trusted_proxies: Sequence[str]) -> str:
    """The client's address, honouring `X-Forwarded-For` only from a trusted peer.

    Returns :data:`UNKNOWN_IP` when there is no peer address to fall back on.
    That string is deliberately not a valid IP: it still works as a throttle
    key, but it can never collide with a real address, and it is obvious in an
    audit record that the source was not established.
    """
    peer = request.client.host if request.client else None
    if peer is None:
        return UNKNOWN_IP

    networks = _networks(trusted_proxies)
    if not _is_trusted(peer, networks):
        # The immediate peer is not a proxy we configured, so anything it
        # claims about earlier hops is unverifiable. This is the correct
        # answer for a direct connection as well as for a spoofing attempt.
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    if not hops:
        return peer

    # Walk right-to-left while each hop is itself a trusted proxy; the first
    # untrusted address is the earliest one the chain vouches for.
    for hop in reversed(hops):
        if not _is_trusted(hop, networks):
            try:
                ipaddress.ip_address(hop)
            except ValueError:
                # A trusted proxy emitted something that is not an address.
                # Fall back to the peer rather than keying a throttle on
                # attacker-chosen text.
                return peer
            return hop

    # Every hop was a trusted proxy — the request never left our own
    # infrastructure, so the leftmost entry is the closest thing to an origin.
    return hops[0]
