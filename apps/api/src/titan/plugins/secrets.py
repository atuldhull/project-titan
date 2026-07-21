"""Secret resolution + config encryption for the plugin runtime.

The implementations moved to the platform-wide vault in Phase 68 (``titan.vault``)
so the secret seam, the field cipher, and HMAC verification have ONE home the
whole platform shares — the bus signer and the identity field-encryption both
reuse the vault, which a domain module like ``titan.plugins`` could not provide
without violating the module-independence contracts. This module re-exports the
same names so every existing plugin-runtime caller is unchanged:

* :class:`SecretsProvider` — the get-by-reference seam (SEC-050); the default
  driver resolves from the environment, a Vault/SOPS driver drops in behind it.
* :class:`ConfigCipher` — field-level AES-256-GCM with a key-version tag
  (SEC-063), still used to encrypt plugin config at rest in ``plugin_installs``.
"""

from titan.vault.cipher import ConfigCipher, FieldCipher, verify_hmac_sha256
from titan.vault.drivers.env import ENV_PREFIX, EnvSecretsProvider
from titan.vault.drivers.static import StaticSecretsProvider
from titan.vault.provider import SecretsProvider

__all__ = [
    "ENV_PREFIX",
    "ConfigCipher",
    "EnvSecretsProvider",
    "FieldCipher",
    "SecretsProvider",
    "StaticSecretsProvider",
    "verify_hmac_sha256",
]
