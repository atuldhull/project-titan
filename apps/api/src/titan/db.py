"""Shared declarative base and table conventions (TITAN-BCK-008).

Every table carries ``created_at`` (server default now()), ``updated_at``
(maintained by the reusable ``set_updated_at`` trigger created in migration
0002), and the optimistic-lock ``version int NOT NULL DEFAULT 1``. Primary
keys are UUIDv7, generated app-side. Statuses are text + Pydantic enums —
never Postgres enums (Vol 3 §6 notes).
"""

import time
import uuid
from collections.abc import Callable
from datetime import datetime

import uuid_utils
from sqlalchemy import MetaData, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint/index naming so hand-written migrations and
# metadata always agree.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def uuid7() -> uuid.UUID:
    """App-side UUIDv7 primary keys (time-ordered, index-friendly)."""
    return uuid.UUID(bytes=uuid_utils.uuid7().bytes)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class ConventionsMixin:
    """created_at / updated_at / version on every table (TITAN-BCK-008)."""

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))


def batched_backfill(
    connection: object,
    update_stmt: str,
    *,
    batch_size: int = 5000,
    sleep_s: float = 0.5,
    params: dict[str, object] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run a data backfill in bounded batches so it never holds a long lock or a huge
    transaction on a live blue/green database (Vol 9 §5.4 / OPS-013: batches of
    <= 5000 rows with sleeps between them).

    ``update_stmt`` MUST be self-limiting on the ``:batch`` bind and idempotent — it
    only ever touches not-yet-backfilled rows, e.g.::

        UPDATE t SET c = :val
        WHERE ctid IN (SELECT ctid FROM t WHERE c IS NULL LIMIT :batch)

    The loop runs it until a batch touches fewer than ``batch_size`` rows (the tail),
    sleeping ``sleep_s`` between batches. Returns the total rows updated. ``sleep`` is
    injected so tests run instantly.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    total = 0
    while True:
        result = connection.execute(  # type: ignore[attr-defined]
            text(update_stmt), {"batch": batch_size, **(params or {})}
        )
        affected = result.rowcount or 0
        total += affected
        if affected < batch_size:
            return total
        sleep(sleep_s)
