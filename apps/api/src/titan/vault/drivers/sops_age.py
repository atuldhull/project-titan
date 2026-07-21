"""The local-default SOPS+age store (ADR-SEC-03, TITAN-SEC-050).

An age-encrypted YAML file, unlocked at boot into an in-memory map — plaintext
lives only in this process's memory, never on disk, in git, in Redis, or in a
Postgres application table. Ciphertext (the ``.age`` file) is git-storable.
Writes (create/rotate/delete) re-encrypt the whole file atomically.

Multi-process freshness: the API process owns writes (the write-only secrets
API), but the worker reads for injection. So ``get``/``names`` re-check the
file's mtime and transparently reload — a rotation in one process is visible to
the other without a restart. Production swaps in Vault/OpenBao (always fresh)
behind the same interface.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
import yaml
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from titan.vault import age

logger = structlog.get_logger("titan.vault.sops_age")


class SopsAgeStore:
    """A :class:`~titan.vault.provider.SecretsStore` backed by an age-encrypted
    YAML file. Constructed with an already-parsed age identity so key handling
    stays at the composition root."""

    def __init__(self, identity: X25519PrivateKey, path: Path) -> None:
        self._identity = identity
        self._recipient = identity.public_key()
        self._path = path
        self._lock = asyncio.Lock()
        self._mtime: float | None = None
        self._values: dict[str, str] = {}
        self._reload_locked()

    # -- load / flush --------------------------------------------------------

    def _reload_locked(self) -> None:
        if not self._path.exists():
            self._values = {}
            self._mtime = None
            return
        mtime = self._path.stat().st_mtime
        if mtime == self._mtime:
            return
        raw = age.decrypt(self._path.read_bytes(), self._identity)
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        if not isinstance(data, dict):
            raise age.AgeError("vault file is not a YAML mapping")
        self._values = {str(k): str(v) for k, v in data.items()}
        self._mtime = mtime

    def _flush_locked(self) -> None:
        plaintext = yaml.safe_dump(self._values, sort_keys=True, default_flow_style=False)
        ciphertext = age.encrypt(plaintext.encode("utf-8"), self._recipient)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_bytes(ciphertext)
        tmp.replace(self._path)  # atomic on POSIX and Windows
        self._mtime = self._path.stat().st_mtime

    # -- SecretsStore --------------------------------------------------------

    async def get(self, name: str) -> str:
        async with self._lock:
            self._reload_locked()
            try:
                return self._values[name]
            except KeyError as exc:
                raise KeyError(name) from exc

    async def put(self, name: str, value: str) -> None:
        async with self._lock:
            self._reload_locked()
            self._values[name] = value
            self._flush_locked()

    async def delete(self, name: str) -> None:
        async with self._lock:
            self._reload_locked()
            self._values.pop(name, None)
            self._flush_locked()

    async def names(self) -> list[str]:
        async with self._lock:
            self._reload_locked()
            return sorted(self._values)


__all__ = ["SopsAgeStore"]
