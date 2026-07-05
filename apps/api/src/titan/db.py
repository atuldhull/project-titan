"""Shared declarative base and table conventions (TITAN-BCK-008).

Every table carries ``created_at`` (server default now()), ``updated_at``
(maintained by the reusable ``set_updated_at`` trigger created in migration
0002), and the optimistic-lock ``version int NOT NULL DEFAULT 1``. Primary
keys are UUIDv7, generated app-side. Statuses are text + Pydantic enums —
never Postgres enums (Vol 3 §6 notes).
"""

import uuid
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
