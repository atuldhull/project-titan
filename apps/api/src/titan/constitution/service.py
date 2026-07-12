"""Public service interface of the ``constitution`` module (Vol 6 §6).

The constitution is the versioned engineering-standards artifact that binds every
TITAN-generated project. Agents retrieve the active version into their system
context via :func:`active_constitution` (they never hardcode §6 into prompts);
gate G5 reads the same version through the handoff GateContext and records the
version number it judged against, so every design gate is auditable against a
specific committed revision.

Only this interface may be imported by other modules. Pydantic v2 DTOs — never ORM
rows — cross the boundary.
"""

import hashlib
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.constitution import repository as repo
from titan.gateway.rbac import not_found


def checksum(body: str) -> str:
    """The canonical constitution checksum: sha256 of the UTF-8 body, hex-encoded.
    Used both to stamp a version and to let a reviewer confirm a retrieved copy is
    byte-intact."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ConstitutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    title: str
    body: str
    checksum: str
    source_ref: str
    created_at: datetime


class ConstitutionHistory(BaseModel):
    versions: list[ConstitutionOut]


async def active_constitution(conn: AsyncConnection) -> ConstitutionOut:
    """The active constitution (highest version) retrieved into context. Raises
    404 only if the seed migration has not run — every deployment ships version 1."""
    row = await repo.active_version(conn)
    if row is None:
        raise not_found("constitution")
    return ConstitutionOut.model_validate(row)


async def get_constitution(conn: AsyncConnection, version: int) -> ConstitutionOut:
    row = await repo.get_version(conn, version)
    if row is None:
        raise not_found("constitution")
    return ConstitutionOut.model_validate(row)


async def constitution_history(conn: AsyncConnection) -> ConstitutionHistory:
    rows = await repo.list_versions(conn)
    return ConstitutionHistory(versions=[ConstitutionOut.model_validate(r) for r in rows])


async def commit_revision(
    conn: AsyncConnection, *, title: str, body: str, source_ref: str
) -> ConstitutionOut:
    """Append a new immutable constitution version (a revision — a new row with the
    next version, never an overwrite). Future governance flows call this to amend
    the standards while preserving every prior version for audit."""
    version = await repo.next_version(conn)
    row = await repo.insert_version(
        conn,
        version=version,
        title=title,
        body=body,
        checksum=checksum(body),
        source_ref=source_ref,
    )
    return ConstitutionOut.model_validate(row)


__all__ = [
    "ConstitutionHistory",
    "ConstitutionOut",
    "active_constitution",
    "checksum",
    "commit_revision",
    "constitution_history",
    "get_constitution",
]
