"""Feature-flag persistence (Phase 72, Vol 9 §5.3, TITAN-OPS-012).

One table, ``feature_flags`` — the source of truth behind the Redis-cached
snapshot. Owned by the leaf ``titan.flags`` module; imports only ``titan.db``
(infrastructure), never a domain module, so the flag store can be read by
``llm`` / ``runtime`` / ``orchestrator`` through ``titan.flags.service`` without
a dependency cycle.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Boolean, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class FeatureFlagRow(ConventionsMixin, Base):
    """One feature flag. ``flag_type`` ∈ boolean|percentage|allowlist and
    ``flag_class`` ∈ release|ops|experiment (both TEXT + CHECK, never PG enums).
    ``enabled`` is the master switch — a disabled flag evaluates false regardless
    of ``percentage``/``allowlist``. ``value`` is an optional scalar payload
    (e.g. ``llm.provider_override``'s target alias)."""

    __tablename__ = "feature_flags"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    flag_type: Mapped[str] = mapped_column(Text, nullable=False)
    flag_class: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'global'"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    percentage: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    allowlist: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )


__all__ = ["FeatureFlagRow"]
