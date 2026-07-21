"""The vault service: create / rotate / delete / list-metadata + audit.

Composes the metadata repository (Postgres) with a writable value
:class:`~titan.vault.provider.SecretsStore` (the age vault). VALUES flow only
into the store; Postgres holds metadata only (SEC-050/SEC-051). Every lifecycle
op writes an audit row carrying the secret NAME, never its value (SEC-092).

Consistency: metadata is written inside the caller's DB transaction, then the
store is updated last. A store failure raises and the DB rolls back (no orphan
metadata). The only residue a crash can leave is a value with no metadata row
(a create whose COMMIT failed after the store write) — ``list``/``get`` never
surface it, and ``delete`` cleans the store unconditionally so it is still
revocable by name (``rotate`` still requires the metadata row and 404s without
it).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import structlog
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.vault import repository as repo
from titan.vault.provider import SecretMetadata, SecretScope, SecretsStore

logger = structlog.get_logger("titan.vault.service")

# Audit event types (§10.2). Values, never secrets, are what these deliberately
# omit (SEC-092).
EVENT_CREATED = "secret.created"
EVENT_ROTATED = "secret.rotated"
EVENT_DELETED = "secret.deleted"
EVENT_INJECTED = "secret.injected"
EVENT_ENVELOPE_REJECTED = "envelope.rejected"

# A secret name is a get-by-reference key that also becomes an injected tmpfs
# filename and an env-var-style lookup — so it must be a safe identifier with no
# path separators or traversal. Upper/lower/digits/._- , 1..128 chars, and never
# "..".
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SecretError(Exception):
    """Base for vault service errors the router maps to a problem response."""


class InvalidSecretName(SecretError):
    """A name that is not a safe get-by-reference identifier."""


class SecretExists(SecretError):
    """Creating a secret whose name is already taken."""


class SecretNotFound(SecretError):
    """Rotating/deleting a secret that does not exist."""


def validate_name(name: str) -> str:
    if ".." in name or not _NAME_RE.match(name):
        raise InvalidSecretName(name)
    return name


def _to_metadata(row: Row[Any]) -> SecretMetadata:
    return SecretMetadata(
        name=row.name,
        scope=SecretScope(row.scope),
        scope_ref=row.scope_ref,
        owner_id=str(row.owner_id) if row.owner_id is not None else None,
        rotation_days=row.rotation_days,
        created_at=row.created_at,
        rotated_at=row.rotated_at,
        updated_at=row.updated_at,
    )


async def create_secret(
    conn: AsyncConnection,
    store: SecretsStore,
    *,
    name: str,
    value: str,
    scope: SecretScope,
    scope_ref: str | None,
    owner_id: Any,
    rotation_days: int,
    actor: str,
    now: dt.datetime | None = None,
) -> SecretMetadata:
    validate_name(name)
    now = now or dt.datetime.now(dt.UTC)
    if await repo.get_metadata(conn, name) is not None:
        raise SecretExists(name)
    row = await repo.insert_metadata(
        conn,
        name=name,
        scope=scope.value,
        scope_ref=scope_ref,
        owner_id=owner_id,
        rotation_days=rotation_days,
        rotated_at=now,
    )
    await repo.insert_audit(
        conn,
        event_type=EVENT_CREATED,
        actor=actor,
        secret_name=name,
        detail={"scope": scope.value, "scope_ref": scope_ref},
    )
    # Store write LAST: a duplicate name or bad metadata rolls back before any
    # value touches the vault file.
    await store.put(name, value)
    logger.info("vault.secret_created", name=name, scope=scope.value)  # never the value
    return _to_metadata(row)


async def rotate_secret(
    conn: AsyncConnection,
    store: SecretsStore,
    *,
    name: str,
    value: str,
    actor: str,
    now: dt.datetime | None = None,
) -> SecretMetadata:
    validate_name(name)
    now = now or dt.datetime.now(dt.UTC)
    row = await repo.touch_rotated(conn, name, now)
    if row is None:
        raise SecretNotFound(name)
    await repo.insert_audit(conn, event_type=EVENT_ROTATED, actor=actor, secret_name=name)
    await store.put(name, value)
    logger.info("vault.secret_rotated", name=name)
    return _to_metadata(row)


async def delete_secret(
    conn: AsyncConnection, store: SecretsStore, *, name: str, actor: str
) -> None:
    validate_name(name)
    existed = await repo.delete_metadata(conn, name)
    if existed:
        await repo.insert_audit(conn, event_type=EVENT_DELETED, actor=actor, secret_name=name)
    # Store cleanup LAST and UNCONDITIONAL: even an orphan value (metadata-less,
    # from a create that crashed at commit) is revoked, so delete-by-name always
    # removes the value. 404 is still returned when there was no metadata row.
    await store.delete(name)
    if not existed:
        raise SecretNotFound(name)
    logger.info("vault.secret_deleted", name=name)


async def list_secret_metadata(conn: AsyncConnection) -> list[SecretMetadata]:
    return [_to_metadata(row) for row in await repo.list_metadata(conn)]


async def get_secret_metadata(conn: AsyncConnection, name: str) -> SecretMetadata | None:
    row = await repo.get_metadata(conn, name)
    return _to_metadata(row) if row is not None else None


async def overdue_secrets(
    conn: AsyncConnection, *, now: dt.datetime | None = None
) -> list[SecretMetadata]:
    """Metadata for every secret whose rotation window has elapsed (SEC-052)."""
    now = now or dt.datetime.now(dt.UTC)
    return [m for m in await list_secret_metadata(conn) if m.is_overdue(now=now)]


async def record_injection(
    conn: AsyncConnection, *, actor: str, names: list[str], task_id: str | None = None
) -> None:
    """Audit a scoped injection of ``names`` into a workload (§10.2 'inject —
    never values'). One row per injection event, listing the names only."""
    if not names:
        return
    await repo.insert_audit(
        conn,
        event_type=EVENT_INJECTED,
        actor=actor,
        secret_name=None,
        detail={"names": sorted(names), "task_id": task_id},
    )


async def record_envelope_rejection(
    engine: AsyncEngine,
    *,
    actor: str,
    topic: str,
    reason: str,
    event_id: str | None,
    payload_digest: str | None,
) -> None:
    """Audit a dropped queue envelope (SEC-020). Own transaction — the bus calls
    this from a background loop with no ambient session. Carries a digest, never
    the payload (SEC-092)."""
    async with engine.begin() as conn:
        await repo.insert_audit(
            conn,
            event_type=EVENT_ENVELOPE_REJECTED,
            actor=actor,
            secret_name=None,
            detail={
                "topic": topic,
                "reason": reason,
                "event_id": event_id,
                "payload_sha256": payload_digest,
            },
        )


__all__ = [
    "EVENT_CREATED",
    "EVENT_DELETED",
    "EVENT_ENVELOPE_REJECTED",
    "EVENT_INJECTED",
    "EVENT_ROTATED",
    "InvalidSecretName",
    "SecretError",
    "SecretExists",
    "SecretNotFound",
    "create_secret",
    "delete_secret",
    "get_secret_metadata",
    "list_secret_metadata",
    "overdue_secrets",
    "record_envelope_rejection",
    "record_injection",
    "validate_name",
]
