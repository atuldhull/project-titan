"""LLM gateway tables (Vol 7 §8.4, TITAN-FND-021, TITAN-OPS-021).

``llm_usage`` is the append-only accounting + audit ledger: one row per served
call with the provider/model that actually served it, token counts, computed
cost, and the fallback/degraded flags — so a substitution is visible in the
audit trail (§8.3) and spend reconciles against provider-reported usage (§8.4).

``budget_holds`` is the persisted pause flag: a project- or task-scoped hold that
the gateway checks pre-request to hard-stop new calls at 100% (TITAN-OPS-021).
"""

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, Integer, Numeric, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class LlmUsage(ConventionsMixin, Base):
    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Routing key (e.g. 'agent.ceo'), not a users/agents FK.
    agent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_alias: Mapped[str | None] = mapped_column(Text, nullable=True)
    served_alias: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 6), nullable=False, server_default=text("0")
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    degraded_model: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    price_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class BudgetHold(ConventionsMixin, Base):
    __tablename__ = "budget_holds"
    __table_args__ = (UniqueConstraint("scope", "scope_id", name="uq_budget_holds_scope"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    scope: Mapped[str] = mapped_column(Text, nullable=False)  # 'project' | 'task'
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
