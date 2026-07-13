"""Technical-debt ledger persistence (Vol 6 §5, TITAN-WFL-021). Private to the
module — only ``service`` crosses the boundary. A debt ticket is *mutable* (its
status moves open → paid, and a paydown task may be linked), so it carries the
TITAN-BCK-008 convention columns and the shared ``set_updated_at`` trigger."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, uuid7


class DebtTicket(Base):
    """One technical-debt item. Keyed uniquely by ``(project_id, fingerprint)`` so
    re-submitting the same accepted deviation (a re-run gate) reuses its ticket
    rather than accreting duplicates — the WFL-021 'every ACWC has A ticket'
    invariant stays exact."""

    __tablename__ = "debt_tickets"
    __table_args__ = (
        UniqueConstraint("project_id", "fingerprint", name="uq_debt_tickets_project_fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # code_review | security | constitution — where the accepted deviation came from.
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # low | medium | high — the debt's priority for paydown ranking.
    severity: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'medium'"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # The proposed paydown work (a future-sprint task description).
    paydown_proposal: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # The real backlog task opened to pay this down, once scheduled (nullable).
    paydown_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    # A stable dedup key (e.g. 'code_review:v3:comment#1') — the idempotency handle.
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    # open | paid
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
