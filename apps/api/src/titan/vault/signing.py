"""HMAC-SHA256 signing for Redis Streams envelopes (TITAN-SEC-020).

Every stream message carries a signed envelope; consumers drop and audit an
invalid signature. The signer is a leaf so ``titan.events`` can hold one without
importing a domain module — the event bus takes a ``Signer`` (structurally this
class) and an invalid-envelope hook, both injected at the composition root.

Key handling mirrors the rest of the platform's "empty = dev" secrets, with one
deliberate difference: an EMPTY key derives a FIXED, well-known dev key rather
than an ephemeral per-process one. A per-process key would make every
cross-process message (the API signs, the worker verifies) fail verification, so
the bus would wedge in local dev. The fixed dev key keeps sign/verify exercised
everywhere; it is loudly warned about and is NOT security — production must set
``TITAN_BUS_HMAC_KEY`` from the vault (a class-1 platform secret, §6.1).
"""

import hashlib
import hmac
from collections.abc import Iterable

import structlog

logger = structlog.get_logger("titan.vault.signing")

# Domain-separated fixed dev key material used only when no key is configured
# (see module docstring). Never rely on this in production.
_DEV_KEY_MATERIAL = "titan-insecure-dev-bus-hmac-key"


def _derive(material: str) -> bytes:
    return hashlib.sha256(material.encode("utf-8")).digest()


class HmacEnvelopeSigner:
    """Signs/verifies a serialized envelope string with HMAC-SHA256.

    ``verify`` accepts a signature made by the primary key OR any of
    ``extra_keys`` — the seam that lets an operator rotate the bus key without a
    flag-day (publish with the new key while still accepting the old until every
    in-flight message has drained)."""

    def __init__(self, key_material: str, *, extra_key_materials: Iterable[str] = ()) -> None:
        if not key_material:
            logger.warning("vault.bus_hmac_key_dev")
            key_material = _DEV_KEY_MATERIAL
        self._sign_key = _derive(key_material)
        # Accept the primary key plus any rotation-window predecessors.
        self._verify_keys = [self._sign_key, *(_derive(m) for m in extra_key_materials if m)]

    def sign(self, payload: str) -> str:
        return hmac.new(self._sign_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, payload: str, signature: str) -> bool:
        if not signature:
            return False
        body = payload.encode("utf-8")
        provided = signature.strip()
        # Constant-time against EACH accepted key. `any()` short-circuits, but a
        # forged signature exercises every compare_digest at full width — the
        # timing signal is which key matched, not whether the digest is close.
        return any(
            hmac.compare_digest(hmac.new(key, body, hashlib.sha256).hexdigest(), provided)
            for key in self._verify_keys
        )


__all__ = ["HmacEnvelopeSigner"]
