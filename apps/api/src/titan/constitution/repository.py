"""Private data access for the constitution (``constitution_versions``).
SQLAlchemy Core over an AsyncConnection; only ``service`` is importable across the
module boundary."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7

_COLS = "id, version, title, body, checksum, source_ref, created_at"


async def active_version(conn: AsyncConnection) -> Row[Any] | None:
    """The active constitution = the row with the highest ``version`` (the latest
    committed revision). None only before the seed migration has run."""
    result = await conn.execute(
        text(f"SELECT {_COLS} FROM constitution_versions ORDER BY version DESC LIMIT 1")
    )
    return result.one_or_none()


async def get_version(conn: AsyncConnection, version: int) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_COLS} FROM constitution_versions WHERE version = :v"),
        {"v": version},
    )
    return result.one_or_none()


async def list_versions(conn: AsyncConnection) -> Sequence[Row[Any]]:
    """Every constitution version, newest first — the audit/history view."""
    result = await conn.execute(
        text(f"SELECT {_COLS} FROM constitution_versions ORDER BY version DESC")
    )
    return result.all()


async def next_version(conn: AsyncConnection) -> int:
    result = await conn.execute(
        text("SELECT COALESCE(MAX(version), 0) + 1 FROM constitution_versions")
    )
    return int(result.scalar_one())


async def insert_version(
    conn: AsyncConnection,
    *,
    version: int,
    title: str,
    body: str,
    checksum: str,
    source_ref: str,
) -> Row[Any]:
    """Append a new immutable constitution version (a revision — never an
    overwrite). The append-only trigger forbids mutating any prior row."""
    result = await conn.execute(
        text(
            "INSERT INTO constitution_versions (id, version, title, body, checksum, source_ref) "
            "VALUES (:id, :version, :title, :body, :checksum, :source_ref) "
            f"RETURNING {_COLS}"
        ),
        {
            "id": uuid7(),
            "version": version,
            "title": title,
            "body": body,
            "checksum": checksum,
            "source_ref": source_ref,
        },
    )
    return result.one()


__all__ = [
    "active_version",
    "get_version",
    "insert_version",
    "list_versions",
    "next_version",
]
