"""Secret resolution + config encryption for the plugin runtime (Vol 7 §3.3/§3.4;
Vol 8 TITAN-SEC-050/053/063).

Two distinct things, deliberately separated:

* **Secrets** are referenced by NAME only — never stored in the manifest, never in an
  application table (SEC-050). :class:`SecretsProvider` is the get-by-reference seam;
  the default driver resolves from the environment (the same ``${ENV_VAR}`` idiom the
  LLM gateway config already uses). A Vault/SOPS driver drops in behind the same
  Protocol without touching callers.
* **Plugin config** is operator-supplied settings data that DOES live in Postgres, so
  it is encrypted at rest with field-level AES-256-GCM and a key-version tag
  (SEC-063), which keeps it distinct from the secret store and makes key rotation a
  re-encrypt rather than a schema change.
"""

import base64
import hmac
import json
import os
from hashlib import sha256
from typing import Any, Protocol

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = structlog.get_logger("titan.plugins.secrets")

#: Env prefix a plugin secret resolves from, e.g. JIRA_API_TOKEN →
#: TITAN_PLUGIN_SECRET_JIRA_API_TOKEN (falling back to the bare name).
ENV_PREFIX = "TITAN_PLUGIN_SECRET_"
_KEY_VERSION = 1
_NONCE_BYTES = 12


class SecretsProvider(Protocol):
    """Vol 8's get-by-reference secret seam. Only ``get`` is needed by the runtime."""

    async def get(self, name: str) -> str:
        """The secret's value, or raise :class:`KeyError` when it is not set."""
        ...


class EnvSecretsProvider:
    """The local-first default driver: secrets come from the process environment and
    are never persisted by TITAN. Production swaps in a Vault/SOPS driver behind the
    same Protocol (configuration-only, mirroring the LLM-gateway philosophy)."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._env = environ if environ is not None else dict(os.environ)

    async def get(self, name: str) -> str:
        for key in (f"{ENV_PREFIX}{name}", name):
            value = self._env.get(key)
            if value:
                return value
        raise KeyError(name)


class StaticSecretsProvider:
    """An explicit in-memory map — used by tests and by an operator-unlocked
    in-memory store."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = dict(values)

    async def get(self, name: str) -> str:
        try:
            return self._values[name]
        except KeyError as exc:
            raise KeyError(name) from exc


# --- config encryption at rest (SEC-063) -------------------------------------


def _derive_key(material: str) -> bytes:
    """A 32-byte AES key from the configured material. An empty setting yields an
    ephemeral dev key — config written with it does not survive a restart, which is
    the intended pressure to configure one in production."""
    if not material:
        logger.warning("plugins.config_key_ephemeral")
        return AESGCM.generate_key(bit_length=256)
    raw = base64.b64decode(material) if len(material) >= 44 else material.encode("utf-8")
    return sha256(raw).digest()  # always exactly 32 bytes


class ConfigCipher:
    """Field-level AES-256-GCM with a key-version tag (SEC-063). The blob is
    ``v<version>.<b64 nonce>.<b64 ciphertext>`` so a rotation can decrypt old versions
    while writing new ones."""

    def __init__(self, key_material: str, *, version: int = _KEY_VERSION) -> None:
        self._key = _derive_key(key_material)
        self._version = version

    def encrypt(self, config: dict[str, Any]) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        plaintext = json.dumps(config, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext, None)
        return (
            f"v{self._version}."
            f"{base64.b64encode(nonce).decode()}."
            f"{base64.b64encode(ciphertext).decode()}"
        )

    def decrypt(self, blob: str) -> dict[str, Any]:
        if not blob:
            return {}
        try:
            version, nonce_b64, ct_b64 = blob.split(".", 2)
            if not version.startswith("v"):
                raise ValueError("missing key-version tag")
            nonce = base64.b64decode(nonce_b64)
            ciphertext = base64.b64decode(ct_b64)
            plaintext = AESGCM(self._key).decrypt(nonce, ciphertext, None)
        except Exception as exc:  # noqa: BLE001 — a tampered/rotated blob is not fatal
            raise ValueError(f"plugin config could not be decrypted: {exc}") from exc
        decoded = json.loads(plaintext.decode("utf-8"))
        return dict(decoded) if isinstance(decoded, dict) else {}


# --- webhook signature verification (§3.2 webhooks.verify) -------------------


def verify_hmac_sha256(secret: str, body: bytes, signature: str) -> bool:
    """Constant-time HMAC-SHA256 verification of a webhook payload. Accepts a bare
    hex digest or the common ``sha256=<hex>`` prefix form."""
    if not secret or not signature:
        return False
    provided = signature.split("=", 1)[1] if signature.startswith("sha256=") else signature
    expected = hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip())


__all__ = [
    "ENV_PREFIX",
    "ConfigCipher",
    "EnvSecretsProvider",
    "SecretsProvider",
    "StaticSecretsProvider",
    "verify_hmac_sha256",
]
