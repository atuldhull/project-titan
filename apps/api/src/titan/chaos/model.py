"""Result types for the chaos suite (TITAN-FND-052 / TITAN-FND-007).

Pydantic (not bare dataclasses) so a run's outcome serializes straight into the
Section-7 KPI report (crash-recovery metric) and the operator dashboard. Every
field is a durable, checkable fact — no free-text pass/fail hand-waving.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

#: The §9.1 / TITAN-FND-052 local-recovery budget: a project must resume within
#: this many seconds of the killed service being restarted.
RESUME_BUDGET_S: float = 60.0


class KillTarget(StrEnum):
    """The five services whose failure the chaos suite SIMULATES in turn (TITAN-FND-
    052 "kill any one service"; the simulations model the failure point in-process,
    not process death). ``orchestrator`` and ``worker`` are distinct roles over the
    reclaim/redrain recovery paths; ``redis`` and ``postgres`` prove the queue-
    redelivery and transactional-system-of-record halves of the guarantee;
    ``backend_api`` proves the buffer resume path a reconnecting WS client uses."""

    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"
    BACKEND_API = "backend_api"
    REDIS = "redis"
    POSTGRES = "postgres"


class ReconciliationReport(BaseModel):
    """Post-chaos ledger reconciliation (DoD item 2): the durable event ledger
    (PostgreSQL ``outbox`` — the system of record) compared against stream
    acknowledgements (``event_inbox``) and the in-flight pending set.

    ``dropped`` is the headline: a committed event that is neither acknowledged
    nor still pending redelivery has been LOST. It MUST be empty."""

    group: str
    durable: int = Field(description="committed events in the system of record")
    acknowledged: int = Field(description="deliveries acked into the inbox ledger")
    pending: int = Field(description="in-flight deliveries awaiting redelivery")
    dropped: list[str] = Field(
        default_factory=list,
        description="committed events neither acked nor pending — acknowledged loss",
    )
    orphaned: list[str] = Field(
        default_factory=list,
        description="acked deliveries with no durable source — phantom acks",
    )

    @property
    def acknowledged_task_loss(self) -> int:
        """Zero-acked-task-loss is the DoD assertion (item 1 & 2)."""
        return len(self.dropped)

    @property
    def reconciled(self) -> bool:
        return not self.dropped and not self.orphaned


class ChaosResult(BaseModel):
    """The outcome of killing one service mid-project and recovering."""

    target: KillTarget
    acknowledged_loss: int = Field(
        default=0, description="committed records lost across the kill — MUST be 0"
    )
    redelivered: int = Field(
        default=0, description="in-flight work items redelivered after the kill"
    )
    duplicated: int = Field(
        default=0, description="side effects produced more than once — MUST be 0"
    )
    resume_seconds: float = Field(
        default=0.0,
        description="recovery-work latency (reclaim/redrain/replay); excludes external restart",
    )
    history_intact: bool = Field(
        default=False, description="message history is complete after recovery"
    )
    detail: str = ""

    @property
    def resumed_within_budget(self) -> bool:
        return self.resume_seconds <= RESUME_BUDGET_S

    @property
    def survived(self) -> bool:
        """The service kill is survived iff nothing acknowledged was lost, no side
        effect duplicated, history is intact, and resume met the local budget."""
        return (
            self.acknowledged_loss == 0
            and self.duplicated == 0
            and self.history_intact
            and self.resumed_within_budget
        )


class ChaosRunReport(BaseModel):
    """The full sweep: one :class:`ChaosResult` per service plus the reconciliation."""

    results: list[ChaosResult] = Field(default_factory=list)
    reconciliation: ReconciliationReport | None = None

    @property
    def crash_recovery_rate(self) -> float:
        """Fraction of service kills fully recovered — the Section-7 "Crash
        recovery" KPI (MUST be 1.0)."""
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.survived) / len(self.results)

    @property
    def all_passed(self) -> bool:
        reconciled = self.reconciliation.reconciled if self.reconciliation else False
        return bool(self.results) and reconciled and all(r.survived for r in self.results)


__all__ = [
    "RESUME_BUDGET_S",
    "ChaosResult",
    "ChaosRunReport",
    "KillTarget",
    "ReconciliationReport",
]
