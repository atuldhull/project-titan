"""Watchdog configuration table (Vol 2 §4.2/§6.3, Phase 32).

The Orchestrator watchdog's timeouts and thresholds live in the DB and are read
fresh every sweep, so a change takes effect without a redeploy (TITAN-AGT-020).
``project_id`` NULL is the org-wide default row; a project row overrides it.
"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class WatchdogConfigRow(ConventionsMixin, Base):
    __tablename__ = "watchdog_config"
    __table_args__ = (
        Index(
            "uq_watchdog_config_scope",
            text("COALESCE(project_id, '00000000-0000-0000-0000-000000000000'::uuid)"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    # A supervised consumer whose heartbeat is older than this is replaced. A DEAD
    # consumer is detected immediately regardless; this catches HUNG ones, so the
    # default exceeds the worst legitimate turn (soft timeout 900 s + slack).
    stalled_after_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("960000")
    )
    sweep_interval_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("5000")
    )
    reclaim_min_idle_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("960000")
    )
    # Per-type requires_ack timeout overrides (message type -> ms); a type absent
    # here uses the code default (messaging bus ACK_TIMEOUTS_MS).
    ack_timeout_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
