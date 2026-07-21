"""Composition-root builders for the vault (Phase 68).

Kept out of ``main``/``worker`` so the driver selection (age vault vs env),
the field cipher, and the bus signer are constructed one way and unit-testable.
Imports only leaf/infra modules — never the router — so nothing here drags a
domain import into a place that must stay pure.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from titan.settings import Settings
from titan.vault import age
from titan.vault.cipher import FieldCipher
from titan.vault.drivers.env import EnvSecretsProvider
from titan.vault.drivers.sops_age import SopsAgeStore
from titan.vault.provider import SecretsProvider, SecretsStore
from titan.vault.signing import HmacEnvelopeSigner

logger = structlog.get_logger("titan.vault.bootstrap")


def build_secret_backend(settings: Settings) -> tuple[SecretsStore | None, SecretsProvider]:
    """Return ``(writable_store, read_provider)``.

    With ``vault_age_key`` set, the age vault is BOTH — one object serves
    create/rotate/delete (store) and get-by-reference (provider). Without it,
    there is no writable store (the write API answers 503) and secrets resolve
    read-only from the environment — the Phase-52 default (SEC-050)."""
    if settings.vault_age_key:
        identity = age.parse_identity(settings.vault_age_key)
        store = SopsAgeStore(identity, Path(settings.vault_age_file))
        logger.info("vault.age_store_unlocked", path=settings.vault_age_file)
        return store, store
    logger.info("vault.env_provider")  # no writable store; env-backed reads
    return None, EnvSecretsProvider()


def build_field_cipher(settings: Settings) -> FieldCipher:
    """The AES-256-GCM cipher for field-level column encryption (SEC-063). The
    previous key (if set) is accepted on decrypt so a rotation window can read
    old rows while writing new ones with the current key."""
    return FieldCipher(
        settings.field_encryption_key,
        extra_key_materials=[settings.field_encryption_key_previous],
    )


def build_envelope_signer(settings: Settings) -> HmacEnvelopeSigner:
    """The HMAC signer for Redis Streams envelopes (SEC-020)."""
    return HmacEnvelopeSigner(settings.bus_hmac_key)


__all__ = [
    "build_envelope_signer",
    "build_field_cipher",
    "build_secret_backend",
]
