"""Client-IP resolution behind the BFF (Phase 67; TITAN-SEC-017).

These tests exist because per-IP throttling is only as honest as the address it
keys on, and the address was not available at all before this phase: nothing
read `X-Forwarded-For`, so every browser user looked like the BFF.

The spoofing cases are the point. A header the API trusts unconditionally is
not a client identifier, it is a throttle-bypass switch — one header per
request and every attempt looks like a new address.
"""

from __future__ import annotations

import ipaddress

import pytest
from starlette.datastructures import Address, Headers

from titan.gateway.client_ip import UNKNOWN_IP, resolve_client_ip
from titan.settings import Settings


class _FakeRequest:
    """The two attributes `resolve_client_ip` reads, without an ASGI scope.

    Starlette's Request needs a full scope to construct, and TestClient fixes
    the peer address at construction time — which is precisely why a per-IP
    test driven through TestClient would be vacuous. Driving the resolver
    directly is what makes these assertions mean something.
    """

    def __init__(self, peer: str | None, headers: dict[str, str] | None = None) -> None:
        self.client = Address(host=peer, port=0) if peer is not None else None
        self.headers = Headers(headers or {})


TRUSTED = ["10.0.0.0/8", "172.18.0.5"]


def test_direct_connection_uses_the_socket_peer() -> None:
    request = _FakeRequest("203.0.113.7")
    assert resolve_client_ip(request, TRUSTED) == "203.0.113.7"


def test_forwarded_header_from_an_untrusted_peer_is_ignored() -> None:
    """The spoofing case: an attacker connecting directly cannot claim an address."""
    request = _FakeRequest("203.0.113.7", {"x-forwarded-for": "198.51.100.1"})
    assert resolve_client_ip(request, TRUSTED) == "203.0.113.7"


def test_forwarded_header_from_a_trusted_proxy_is_honoured() -> None:
    request = _FakeRequest("10.0.0.3", {"x-forwarded-for": "198.51.100.1"})
    assert resolve_client_ip(request, TRUSTED) == "198.51.100.1"


def test_a_forged_prefix_cannot_displace_what_the_proxy_observed() -> None:
    """The BFF appends what it saw; anything to its left is client-supplied.

    Reading `xff[0]` — the obvious implementation — returns the attacker's
    string here. Walking right-to-left returns the address the trusted hop
    actually vouched for.
    """
    request = _FakeRequest(
        "10.0.0.3",
        {"x-forwarded-for": "1.2.3.4, 5.6.7.8, 198.51.100.1"},
    )
    assert resolve_client_ip(request, TRUSTED) == "198.51.100.1"


def test_chain_of_trusted_hops_walks_back_to_the_first_untrusted_address() -> None:
    request = _FakeRequest(
        "10.0.0.3",
        {"x-forwarded-for": "198.51.100.1, 10.0.0.9, 172.18.0.5"},
    )
    assert resolve_client_ip(request, TRUSTED) == "198.51.100.1"


def test_all_hops_trusted_falls_back_to_the_leftmost() -> None:
    """Wholly-internal traffic: no untrusted hop exists, so the origin is hop 0."""
    request = _FakeRequest("10.0.0.3", {"x-forwarded-for": "10.0.0.9, 172.18.0.5"})
    assert resolve_client_ip(request, TRUSTED) == "10.0.0.9"


def test_empty_trust_list_ignores_the_header_entirely() -> None:
    """The default. Degrades to per-proxy throttling rather than to no throttling."""
    request = _FakeRequest("10.0.0.3", {"x-forwarded-for": "198.51.100.1"})
    assert resolve_client_ip(request, []) == "10.0.0.3"


def test_garbage_from_a_trusted_proxy_falls_back_to_the_peer() -> None:
    """A throttle key must never be attacker-chosen free text."""
    request = _FakeRequest("10.0.0.3", {"x-forwarded-for": "not-an-ip"})
    assert resolve_client_ip(request, TRUSTED) == "10.0.0.3"


def test_missing_peer_is_reported_as_unknown_not_guessed() -> None:
    request = _FakeRequest(None, {"x-forwarded-for": "198.51.100.1"})
    assert resolve_client_ip(request, TRUSTED) == UNKNOWN_IP


def test_unknown_can_never_collide_with_a_real_address() -> None:
    with pytest.raises(ValueError):
        ipaddress.ip_address(UNKNOWN_IP)


def test_ipv6_peer_and_forwarded_address() -> None:
    request = _FakeRequest("fd00::1", {"x-forwarded-for": "2001:db8::5"})
    assert resolve_client_ip(request, ["fd00::/8"]) == "2001:db8::5"


def test_bare_host_entry_is_treated_as_a_single_address() -> None:
    request = _FakeRequest("172.18.0.5", {"x-forwarded-for": "198.51.100.1"})
    assert resolve_client_ip(request, ["172.18.0.5"]) == "198.51.100.1"
    # ...and its neighbour is NOT trusted by that entry.
    neighbour = _FakeRequest("172.18.0.6", {"x-forwarded-for": "198.51.100.1"})
    assert resolve_client_ip(neighbour, ["172.18.0.5"]) == "172.18.0.6"


def test_settings_reject_a_malformed_cidr_at_startup() -> None:
    """Fail closed and loudly: a typo would otherwise silently disable trust."""
    with pytest.raises(ValueError, match="not a valid IP address or CIDR"):
        Settings(trusted_proxies=["10.0.0.0/33"])


def test_settings_parse_the_comma_separated_form() -> None:
    settings = Settings(trusted_proxies="10.0.0.0/8, 172.18.0.5")
    assert settings.trusted_proxies == ["10.0.0.0/8", "172.18.0.5"]
