"""Artifact storage abstraction (Vol 3 §6, Phase 12).

Artifacts persist their bytes THROUGH this ``Storage`` protocol and keep only an
opaque ``storage_ref`` in Postgres — handlers never build raw filesystem paths,
so an object-store backend swaps in later without touching callers. The MVP
implementation is a local filesystem tree.
"""

import asyncio
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Storage(Protocol):
    async def put(self, key: str, data: bytes) -> str:
        """Persist bytes under ``key``; return the opaque storage_ref."""
        ...

    async def get(self, storage_ref: str) -> bytes:
        """Read back the bytes for a storage_ref produced by :meth:`put`."""
        ...

    def location(self, storage_ref: str) -> str:
        """An opaque locator for the ref (never a raw local path)."""
        ...


class FilesystemStorage:
    """Local-filesystem backend (MVP). storage_ref is ``fs:<key>``; the physical
    root stays private so it never leaks into an API response or a handler."""

    scheme = "fs"

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()

    async def put(self, key: str, data: bytes) -> str:
        path = self._safe_path(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        return f"{self.scheme}:{key}"

    async def get(self, storage_ref: str) -> bytes:
        path = self._safe_path(self._key(storage_ref))
        return await asyncio.to_thread(path.read_bytes)

    def location(self, storage_ref: str) -> str:
        # Opaque by design: a future download route resolves this via get(),
        # so callers never receive (or construct) a physical path.
        return storage_ref

    def _key(self, storage_ref: str) -> str:
        scheme, sep, key = storage_ref.partition(":")
        if sep != ":" or scheme != self.scheme or not key:
            raise ValueError(f"not a {self.scheme}: storage ref: {storage_ref!r}")
        return key

    def _safe_path(self, key: str) -> Path:
        # Contain every key under the root — reject traversal out of the tree.
        path = (self._root / key).resolve()
        if path != self._root and self._root not in path.parents:
            raise ValueError(f"storage key escapes the root: {key!r}")
        return path
