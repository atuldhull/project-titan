"""Field-level AES-256-GCM encryption + HMAC verification (TITAN-SEC-063).

The cipher that Phase 52 shipped as ``titan.plugins.secrets.ConfigCipher`` lives
here now, generalised from "encrypt a dict" to "encrypt bytes/str/dict" so the
same key-versioned blob format protects the highest-sensitivity columns
(mfa_credentials.secret today; stored OAuth tokens / webhook secrets when a
connector persists one) as well as plugin config. ``ConfigCipher`` remains a
name alias so every existing caller keeps working.

Blob format is ``v<version>.<b64 nonce>.<b64 ciphertext>``. Key rotation is real
(TITAN-SEC-063 acceptance "rotation test decrypts old rows with old key
version"): a :class:`FieldCipher` encrypts with its PRIMARY key and decrypts by
trying the primary then each ``extra_key_materials`` predecessor, exactly like
:class:`~titan.vault.signing.HmacEnvelopeSigner`'s verify-key window. To rotate,
set the new key as ``field_encryption_key`` and the old one as
``field_encryption_key_previous``: new writes use the new key, old rows still
decrypt under the old. (AES-GCM does not authenticate the ``v<n>`` tag, so
decrypt cannot trust it to select a key — it tries each accepted key, which the
GCM tag itself disambiguates.)
"""

import base64
import hmac
import json
import os
from collections.abc import Iterable
from hashlib import sha256
from typing import Any

import structlog
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = structlog.get_logger("titan.vault.cipher")

_KEY_VERSION = 1
_NONCE_BYTES = 12


def derive_key(material: str) -> bytes:
    """A 32-byte AES key from the configured material. An empty setting yields an
    ephemeral dev key — anything written with it does not survive a restart. This
    is reachable ONLY in local: staging/production refuse to boot on an empty
    field_encryption_key (Settings._require_crypto_keys_outside_local), because an
    ephemeral key would lock every MFA user out after a restart."""
    if not material:
        logger.warning("vault.field_key_ephemeral")
        return AESGCM.generate_key(bit_length=256)
    raw = base64.b64decode(material) if len(material) >= 44 else material.encode("utf-8")
    return sha256(raw).digest()  # always exactly 32 bytes


class FieldCipher:
    """Field-level AES-256-GCM with a key-version tag (TITAN-SEC-063).

    ``encrypt``/``decrypt`` keep the historical dict interface (plugin config);
    ``encrypt_str``/``decrypt_str`` and ``encrypt_bytes``/``decrypt_bytes`` are
    the general forms Phase 68 uses for single-value columns.
    """

    def __init__(
        self,
        key_material: str,
        *,
        version: int = _KEY_VERSION,
        extra_key_materials: Iterable[str] = (),
    ) -> None:
        self._key = derive_key(key_material)
        self._version = version
        # Decrypt tries the primary first, then rotation predecessors — the seam
        # that lets a key rotation read old rows while writing new ones.
        self._decrypt_keys = [self._key, *(derive_key(m) for m in extra_key_materials if m)]

    # -- primitive bytes form ------------------------------------------------

    def encrypt_bytes(self, plaintext: bytes) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, None)
        return (
            f"v{self._version}."
            f"{base64.b64encode(nonce).decode()}."
            f"{base64.b64encode(ciphertext).decode()}"
        )

    def decrypt_bytes(self, blob: str) -> bytes:
        try:
            version, nonce_b64, ct_b64 = blob.split(".", 2)
            if not version.startswith("v"):
                raise ValueError("missing key-version tag")
            nonce = base64.b64decode(nonce_b64)
            ciphertext = base64.b64decode(ct_b64)
        except Exception as exc:  # noqa: BLE001 — a malformed blob is not fatal
            raise ValueError(f"ciphertext could not be decrypted: {exc}") from exc
        # Try each accepted key; the GCM tag disambiguates which one wrote it.
        for key in self._decrypt_keys:
            try:
                return AESGCM(key).decrypt(nonce, ciphertext, None)
            except InvalidTag:
                continue
        raise ValueError("ciphertext could not be decrypted: no accepted key matched")

    def is_ciphertext(self, blob: str) -> bool:
        """A cheap structural check: does ``blob`` look like one of our
        version-tagged blobs? Used to tell an already-encrypted column value
        apart from a legacy plaintext one during a gradual rollout — it never
        proves decryptability, only shape."""
        if not blob:
            return False
        head, _, rest = blob.partition(".")
        if not (len(head) >= 2 and head[0] == "v" and head[1:].isdigit()):
            return False
        return rest.count(".") == 1

    # -- str form ------------------------------------------------------------

    def encrypt_str(self, plaintext: str) -> str:
        return self.encrypt_bytes(plaintext.encode("utf-8"))

    def decrypt_str(self, blob: str) -> str:
        return self.decrypt_bytes(blob).decode("utf-8")

    # -- dict form (back-compat with ConfigCipher) ---------------------------

    def encrypt(self, config: dict[str, Any]) -> str:
        plaintext = json.dumps(config, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return self.encrypt_bytes(plaintext)

    def decrypt(self, blob: str) -> dict[str, Any]:
        if not blob:
            return {}
        decoded = json.loads(self.decrypt_bytes(blob).decode("utf-8"))
        return dict(decoded) if isinstance(decoded, dict) else {}


#: Historical name — Phase 52 shipped the dict form as ``ConfigCipher``.
ConfigCipher = FieldCipher


def verify_hmac_sha256(secret: str, body: bytes, signature: str) -> bool:
    """Constant-time HMAC-SHA256 verification of a webhook payload. Accepts a bare
    hex digest or the common ``sha256=<hex>`` prefix form."""
    if not secret or not signature:
        return False
    provided = signature.split("=", 1)[1] if signature.startswith("sha256=") else signature
    expected = hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip())


__all__ = [
    "ConfigCipher",
    "FieldCipher",
    "derive_key",
    "verify_hmac_sha256",
]
