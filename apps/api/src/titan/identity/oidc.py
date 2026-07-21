"""OAuth2/OIDC service layer (Phase 67; TITAN-SEC-010).

The pure, HTTP-free half of the embedded IdP's Authorization Code + PKCE flow:
PKCE verification (RFC 7636), single-use authorization-code storage in Redis,
grant/response-type validation, and the OIDC discovery document. A router mints
tokens and renders errors; this module never touches FastAPI, the DB, or the
:class:`~titan.identity.tokens.KeyRing`.

**SEC-010 is enforced here, not merely unimplemented.** The password grant
(``grant_type=password``, RFC 6749 §4.3) and the implicit flow
(``response_type=token``) are actively rejected with OAuth errors — see
``docs/authentication-grants.md`` for why local password *mode* is not the
password *grant*. A grant that is absent because nobody wrote it is
indistinguishable from one that is forbidden; only the second survives someone
adding a convenience endpoint later, so the rejection is explicit and tested.

Only S256 PKCE is accepted: ``plain`` relays the verifier unprotected and is
disallowed by policy. Authorization codes are single-use — ``consume`` deletes
before it validates, so a code that fails PKCE or redirect_uri matching is
still burned and cannot be retried (RFC 6749 §10.5, §4.1.3).

Redis keys carry the Phase-8 ``deny:`` sibling style: ``oidc:code:{code}``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

# RFC 7636 §4.1: a code_verifier is 43–128 chars of the unreserved set. We only
# range-check length here; the transform below rejects any non-ASCII byte.
_VERIFIER_MIN_LEN = 43
_VERIFIER_MAX_LEN = 128

CODE_CHALLENGE_METHOD_S256 = "S256"

# The one grant and the one response type this authorization server supports.
# These constants ARE the machine-readable statement of SEC-010: "password" and
# "implicit" are deliberately absent, "token" is deliberately absent.
SUPPORTED_RESPONSE_TYPES = ("code",)
SUPPORTED_GRANT_TYPES = ("authorization_code", "refresh_token")


class OAuthError(Exception):
    """An RFC 6749 §5.2 error the token/authorize endpoints render as 400.

    ``error`` is the registered code (``unsupported_grant_type``,
    ``unsupported_response_type``, ``invalid_grant``, ``invalid_request``,
    ``invalid_client``); ``description`` is human-readable and safe to return.
    """

    def __init__(self, error: str, description: str) -> None:
        super().__init__(f"{error}: {description}")
        self.error = error
        self.description = description

    def to_dict(self) -> dict[str, str]:
        """The RFC 6749 §5.2 JSON body: ``{error, error_description}``."""
        return {"error": self.error, "error_description": self.description}


# --------------------------------------------------------------------------- #
# PKCE (RFC 7636)
# --------------------------------------------------------------------------- #
def compute_s256_challenge(code_verifier: str) -> str:
    """BASE64URL-NO-PAD(SHA256(ASCII(code_verifier))) — RFC 7636 §4.2.

    A helper the authorize endpoint (and the tests) use to derive the challenge
    a client would send. ASCII-encodes the verifier; a non-ASCII verifier is
    outside the RFC's unreserved-character grammar and raises here.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """True iff ``code_verifier`` proves knowledge behind ``code_challenge``.

    S256 only. ``plain`` is rejected (returns False) as a matter of policy: it
    relays the verifier unprotected, defeating the point of PKCE. The verifier
    length is range-checked per RFC 7636 §4.1 before the transform, and the
    comparison is constant-time to avoid leaking the challenge byte by byte.
    """
    if method != CODE_CHALLENGE_METHOD_S256:
        return False
    if not _VERIFIER_MIN_LEN <= len(code_verifier) <= _VERIFIER_MAX_LEN:
        return False
    try:
        expected = compute_s256_challenge(code_verifier)
    except UnicodeEncodeError:
        return False
    return secrets.compare_digest(expected, code_challenge)


# --------------------------------------------------------------------------- #
# Grant / response-type validation (the SEC-010 core)
# --------------------------------------------------------------------------- #
def validate_response_type(response_type: str) -> None:
    """Accept ``code`` only; raise ``unsupported_response_type`` otherwise.

    The implicit flow (``response_type=token``) is named explicitly in the
    rejection so a caller — and SEC-010's acceptance test — sees it is refused
    on purpose, not merely unbuilt.
    """
    if response_type == "code":
        return
    if response_type == "token":
        raise OAuthError(
            "unsupported_response_type",
            "the implicit flow (response_type=token) is disabled; use response_type=code with PKCE",
        )
    raise OAuthError(
        "unsupported_response_type",
        f"response_type {response_type!r} is not supported; only 'code' is",
    )


def validate_grant_type(grant_type: str) -> None:
    """Accept ``authorization_code`` and ``refresh_token``; reject the rest.

    These two are exactly what the discovery document advertises
    (``grant_types_supported``), so they must both pass here — an earlier
    version accepted only ``authorization_code``, which made the token
    endpoint's own refresh branch DEAD CODE (it rejected a grant it advertised).

    The resource-owner password grant (``grant_type=password``) is named in its
    rejection and pointed back at the first-party authorize form: credentials
    are typed into the IdP's own login page, never relayed through a client to
    the token endpoint. ``implicit`` and ``client_credentials`` are likewise
    refused.
    """
    if grant_type in ("authorization_code", "refresh_token"):
        return
    if grant_type == "password":
        raise OAuthError(
            "unsupported_grant_type",
            "the resource-owner password grant is disabled (TITAN-SEC-010); "
            "credentials must be entered on the first-party /authorize login "
            "form, not posted to the token endpoint",
        )
    if grant_type in ("implicit", "client_credentials"):
        raise OAuthError(
            "unsupported_grant_type",
            f"the {grant_type} grant is not supported; use authorization_code with PKCE",
        )
    raise OAuthError(
        "unsupported_grant_type",
        f"grant_type {grant_type!r} is not supported; use 'authorization_code' "
        "(with PKCE) or 'refresh_token'",
    )


# --------------------------------------------------------------------------- #
# Authorization-code store (Redis, single-use, short TTL)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AuthCodePayload:
    """Everything the token exchange needs to mint without re-hitting the DB.

    Bound to the code at ``/authorize`` time and returned by ``consume`` after
    redirect_uri and PKCE checks pass. ``roles`` is carried so the router can
    populate the access token's claims directly.
    """

    user_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    scope: str
    roles: list[str]


class AuthCodeStore:
    """Single-use authorization codes in Redis with a short TTL.

    A code is high-entropy (``secrets.token_urlsafe(32)``), stored as JSON at
    ``oidc:code:{code}``, and consumed atomically: ``consume`` fetch-and-deletes
    so a replayed code always misses, and a code that fails a later check is
    already gone.
    """

    def __init__(self, redis: Redis, ttl_seconds: int = 60) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    @staticmethod
    def _key(code: str) -> str:
        return f"oidc:code:{code}"

    async def issue(self, payload: AuthCodePayload) -> str:
        """Store ``payload`` under a fresh code with the store's TTL; return it."""
        code = secrets.token_urlsafe(32)
        await self._redis.set(self._key(code), json.dumps(asdict(payload)), ex=self._ttl)
        return code

    async def consume(self, code: str, redirect_uri: str, code_verifier: str) -> AuthCodePayload:
        """Atomically burn ``code`` and validate it; return the bound payload.

        The GETDEL is first and unconditional (RFC 6749 §4.1.2, §10.5): a code
        is single-use, so it is deleted before any check runs. A missing code —
        never issued, already consumed, or TTL-expired — is
        ``invalid_grant``. ``redirect_uri`` must match the value bound at
        authorize time exactly (this is what stops an intercepted code being
        redeemed to a different, attacker-controlled URI). PKCE is verified
        last; a failure still leaves the code burned, so it cannot be retried.
        """
        raw = await self._redis.getdel(self._key(code))
        if raw is None:
            raise OAuthError("invalid_grant", "code is invalid or expired")
        payload = _decode_payload(raw)
        if not secrets.compare_digest(payload.redirect_uri, redirect_uri):
            raise OAuthError(
                "invalid_grant", "redirect_uri does not match the authorization request"
            )
        if not verify_pkce(code_verifier, payload.code_challenge, payload.code_challenge_method):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        return payload


def _decode_payload(raw: str | bytes) -> AuthCodePayload:
    """Rebuild an :class:`AuthCodePayload` from its stored JSON.

    Accepts ``str`` or ``bytes`` because the redis client's ``getdel`` return
    type is untyped w.r.t. ``decode_responses``; ``json.loads`` handles both. A
    malformed/tampered blob is treated as an invalid code rather than an
    internal error — the endpoint response is the same ``invalid_grant``.
    """
    try:
        data = json.loads(raw)
        return AuthCodePayload(
            user_id=str(data["user_id"]),
            redirect_uri=str(data["redirect_uri"]),
            code_challenge=str(data["code_challenge"]),
            code_challenge_method=str(data["code_challenge_method"]),
            scope=str(data["scope"]),
            roles=[str(role) for role in data["roles"]],
        )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise OAuthError("invalid_grant", "code is invalid or expired") from exc


# --------------------------------------------------------------------------- #
# OIDC discovery
# --------------------------------------------------------------------------- #
def discovery_document(issuer: str, base_url: str) -> dict[str, object]:
    """A minimal OIDC discovery document (``/.well-known/openid-configuration``).

    ``grant_types_supported`` excludes ``password`` and
    ``response_types_supported`` excludes ``token`` deliberately — that
    exclusion is itself the machine-readable statement of SEC-010. Endpoint
    paths are fixed by the router that owns them.
    """
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{base_url}/api/v1/oauth/authorize",
        "token_endpoint": f"{base_url}/api/v1/oauth/token",
        "jwks_uri": f"{base_url}/api/v1/oauth/jwks",
        "response_types_supported": list(SUPPORTED_RESPONSE_TYPES),
        "grant_types_supported": list(SUPPORTED_GRANT_TYPES),
        "code_challenge_methods_supported": [CODE_CHALLENGE_METHOD_S256],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "profile", "email"],
        "subject_types_supported": ["public"],
    }
