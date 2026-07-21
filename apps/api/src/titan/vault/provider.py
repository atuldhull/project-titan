"""The get-by-reference secret seam + the writable store interface (ADR-SEC-03).

ADR-SEC-03 specifies a thin ``SecretsProvider`` interface — get-by-reference,
rotate, list-metadata. Those split across two roles here:

* :class:`SecretsProvider` is the read seam every consumer uses (plugin secret
  resolution, sandbox injection). Only ``get`` — the hot path never rotates or
  lists. The default driver is env-backed (:class:`~titan.vault.drivers.env.
  EnvSecretsProvider`); the local-default SOPS+age driver and any production
  Vault/OpenBao driver satisfy the same Protocol (configuration-only switching).
* :class:`SecretsStore` adds the write operations (put/delete/names) that the
  vault SERVICE needs to actually rotate a value. list-metadata is served from
  Postgres by the repository, not the store, because §6.1/SEC-051 keep VALUES
  out of Postgres — only metadata lives there.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

#: Rotation period a secret gets when the operator does not set one (SEC-052).
DEFAULT_ROTATION_DAYS = 90


class SecretScope(StrEnum):
    """Who a secret is scoped to (SEC-052). ``org`` secrets inject into any of
    the org's tasks; ``project``/``task`` narrow that."""

    ORG = "org"
    PROJECT = "project"
    TASK = "task"


@runtime_checkable
class SecretsProvider(Protocol):
    """Vol 8's get-by-reference secret seam. Only ``get`` is needed by the
    runtime; a missing secret raises :class:`KeyError`."""

    async def get(self, name: str) -> str: ...


@runtime_checkable
class SecretsStore(SecretsProvider, Protocol):
    """A writable secret backend: the vault service rotates values through this.
    Values NEVER round-trip to any human/API (SEC-051) — ``get`` exists only for
    injection into a workload (§6.3)."""

    async def put(self, name: str, value: str) -> None: ...

    async def delete(self, name: str) -> None: ...

    async def names(self) -> list[str]: ...


class SecretMetadata(BaseModel):
    """The non-secret facts about a stored secret (SEC-052). This is the ONLY
    thing a read API returns — never a value."""

    name: str
    scope: SecretScope
    scope_ref: str | None = None
    owner_id: str | None = None
    rotation_days: int = DEFAULT_ROTATION_DAYS
    created_at: dt.datetime
    rotated_at: dt.datetime
    updated_at: dt.datetime

    @property
    def due_at(self) -> dt.datetime:
        return self.rotated_at + dt.timedelta(days=self.rotation_days)

    def is_overdue(self, *, now: dt.datetime) -> bool:
        return now >= self.due_at


__all__ = [
    "DEFAULT_ROTATION_DAYS",
    "SecretMetadata",
    "SecretScope",
    "SecretsProvider",
    "SecretsStore",
]
