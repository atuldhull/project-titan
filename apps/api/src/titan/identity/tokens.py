"""RS256 access-token minting and verification (TITAN-SEC-011/014).

Access JWTs: ≤ 15 min TTL, claims sub/org/roles/scope/jti (+ iat/exp/aud/
iss, plus fam so revoking a refresh family kills its outstanding access
tokens). Verifiers accept RS256 ONLY — alg=none and HS256 are rejected by
construction. Revocation is a Redis denylist on jti and family, checked per
request (≤ 60 s effect).
"""

import base64
import binascii
import datetime as dt
from typing import Any

import jwt
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from redis.asyncio import Redis

from titan.db import uuid7
from titan.settings import Settings

logger = structlog.get_logger("titan.identity")

ALGORITHM = "RS256"
ACCESS_TTL_SECONDS = 15 * 60  # TITAN-SEC-011 ceiling


class TokenInvalid(Exception):
    """Verification failed (signature, claims, expiry, or revocation)."""


class KeyRing:
    """Holds the RS256 keypair; mints and verifies TITAN's own JWTs."""

    def __init__(self, settings: Settings) -> None:
        self.audience = settings.jwt_audience
        self.issuer = settings.jwt_issuer
        self.org = settings.org
        private_key = _load_or_generate(settings)
        self._private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_key = private_key.public_key()
        self._public_pem = public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        # RS256 by construction (ALGORITHM); the loader only ever yields RSA, so
        # narrow the key-type union for the JWK numbers below.
        assert isinstance(public_key, rsa.RSAPublicKey)
        self._public_numbers = public_key.public_numbers()
        # RFC 7638 JWK thumbprint as the key id. Deriving it from the key
        # material (not a random id) means the same key always presents the same
        # `kid`, so the published JWKS and a minted token's header agree with no
        # extra bookkeeping.
        self._kid = self._compute_kid()

    @staticmethod
    def _b64url_uint(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    def _compute_kid(self) -> str:
        import hashlib
        import json

        # Thumbprint over the REQUIRED members only, lexicographically ordered,
        # no whitespace (RFC 7638 §3.2).
        canonical = json.dumps(
            {
                "e": self._b64url_uint(self._public_numbers.e),
                "kty": "RSA",
                "n": self._b64url_uint(self._public_numbers.n),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(canonical.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    @property
    def kid(self) -> str:
        return self._kid

    def public_jwks(self) -> dict[str, Any]:
        """The public key as a JWK Set (TITAN-SEC-011; OIDC discovery).

        What a verifier — an external OIDC relying party, or TITAN's own WS
        auth — fetches to check an RS256 signature. Only the public numbers are
        exposed; the private key never leaves this object.
        """
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": ALGORITHM,
                    "kid": self._kid,
                    "n": self._b64url_uint(self._public_numbers.n),
                    "e": self._b64url_uint(self._public_numbers.e),
                }
            ]
        }

    def mint(
        self,
        sub: str,
        roles: list[str],
        scope: str,
        family_id: str | None = None,
        agent_id: str | None = None,
        ttl_seconds: int = ACCESS_TTL_SECONDS,
    ) -> tuple[str, str]:
        """Return (token, jti)."""
        now = dt.datetime.now(dt.UTC)
        jti = str(uuid7())
        claims: dict[str, Any] = {
            "sub": sub,
            "org": self.org,
            "roles": roles,
            "scope": scope,
            "jti": jti,
            "iat": now,
            "exp": now + dt.timedelta(seconds=min(ttl_seconds, ACCESS_TTL_SECONDS)),
            "aud": self.audience,
            "iss": self.issuer,
        }
        if family_id:
            claims["fam"] = family_id
        if agent_id:
            claims["agent_id"] = agent_id
        # `kid` in the header so a JWKS consumer can select the right key.
        return (
            jwt.encode(
                claims,
                self._private_pem,
                algorithm=ALGORITHM,
                headers={"kid": self._kid},
            ),
            jti,
        )

    def verify(self, token: str) -> dict[str, Any]:
        """Static verification (signature + claims). Raises TokenInvalid.

        ``algorithms=[RS256]`` is the none/HS256 rejection: PyJWT refuses
        any token whose header alg is not in the allowlist.
        """
        try:
            return jwt.decode(
                token,
                self._public_pem,
                algorithms=[ALGORITHM],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["sub", "org", "roles", "exp", "iat", "aud", "iss", "jti"]},
            )
        except jwt.InvalidTokenError as exc:
            raise TokenInvalid(str(exc)) from exc


# Roles that force a re-authentication on an incident-lockdown epoch revocation
# (TITAN-SEC-120: "force privileged re-auth"). Viewer/operator sessions are not swept.
_PRIVILEGED_ROLES = frozenset({"owner", "admin", "maintainer"})
_AGENTS_EPOCH_KEY = "deny:agents:iat_before"
_PRIVILEGED_EPOCH_KEY = "deny:privileged:iat_before"


def _is_privileged(claims: dict[str, Any]) -> bool:
    return bool(_PRIVILEGED_ROLES.intersection(claims.get("roles") or []))


async def is_revoked(redis: Redis, claims: dict[str, Any]) -> bool:
    """Denylist check on jti, refresh family, and the incident-lockdown epochs
    (TITAN-SEC-014/120). A token is revoked if its jti/fam is denied, OR it is an
    agent token minted before the agents-epoch, OR a privileged token minted before
    the privileged-epoch — the latter two are the one-flip lockdown fleet sweeps."""
    keys = [f"deny:jti:{claims['jti']}"]
    if claims.get("fam"):
        keys.append(f"deny:fam:{claims['fam']}")
    epoch_keys: list[str] = []
    if claims.get("agent_id"):
        epoch_keys.append(_AGENTS_EPOCH_KEY)
    if _is_privileged(claims):
        epoch_keys.append(_PRIVILEGED_EPOCH_KEY)
    hits = await redis.mget(keys + epoch_keys)
    if any(hit is not None for hit in hits[: len(keys)]):
        return True
    iat = int(claims.get("iat", 0))
    # An epoch hit revokes the token if it was minted BEFORE the stored cutoff.
    return any(hit is not None and iat < int(hit) for hit in hits[len(keys) :])


async def revoke_jti(redis: Redis, jti: str, ttl_seconds: int = ACCESS_TTL_SECONDS) -> None:
    await redis.set(f"deny:jti:{jti}", "1", ex=ttl_seconds)


async def revoke_family(redis: Redis, family_id: str, ttl_seconds: int = 7 * 86400) -> None:
    await redis.set(f"deny:fam:{family_id}", "1", ex=ttl_seconds)


async def revoke_agents_before(
    redis: Redis, cutoff_epoch: int, ttl_seconds: int = 7 * 86400
) -> None:
    """Revoke EVERY agent service token minted before ``cutoff_epoch`` (unix secs) —
    the incident-lockdown "revoke agent service tokens" fan-out (TITAN-SEC-120)."""
    await redis.set(_AGENTS_EPOCH_KEY, str(int(cutoff_epoch)), ex=ttl_seconds)


async def revoke_privileged_before(
    redis: Redis, cutoff_epoch: int, ttl_seconds: int = 7 * 86400
) -> None:
    """Force re-auth of every privileged (owner/admin/maintainer) session minted
    before ``cutoff_epoch`` — the lockdown "force privileged re-auth" (TITAN-SEC-120)."""
    await redis.set(_PRIVILEGED_EPOCH_KEY, str(int(cutoff_epoch)), ex=ttl_seconds)


def _load_or_generate(settings: Settings) -> PrivateKeyTypes:
    if settings.jwt_private_key_b64:
        try:
            pem = base64.b64decode(settings.jwt_private_key_b64)
        except binascii.Error as exc:
            raise ValueError("TITAN_JWT_PRIVATE_KEY_B64 is not valid base64") from exc
        return serialization.load_pem_private_key(pem, password=None)
    # Dev convenience only: ephemeral keys mean every restart invalidates
    # all outstanding tokens. Set TITAN_JWT_PRIVATE_KEY_B64 for stable keys.
    logger.warning("jwt.ephemeral_keypair", detail="set TITAN_JWT_PRIVATE_KEY_B64 to persist")
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)
