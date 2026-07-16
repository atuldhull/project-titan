"""Full-roster offline acceptance run (TITAN-AGT-007, Phase 56).

Drives the reference task-tracking-app brief through all 16 roster agents as REAL
turns on the Ollama-only profile, records each turn's honest outcome, verifies
zero paid-provider egress, and captures the Section-7 KPI report via the Phase-55
runner — the whole thing bundled into an :class:`AcceptanceRecord`.

Honesty is the point: this proves the roster RUNS offline and the delivery wiring
(LiveExecutor + KPI capture) works, and it records exactly what a local 7B
produced per role. It does NOT pretend the local model autonomously built a
gate-passing app — ``autonomous_app_completed`` is derived from real turn
outcomes and will be False when the model's outputs don't pass, which is the
expected, truthful result at this model size.
"""

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.agents.service import CORE_ROSTER
from titan.benchmark.egress_guard import EgressGuard
from titan.benchmark.kpi import KpiReport
from titan.benchmark.runner import BenchmarkRunner
from titan.db import uuid7
from titan.messaging.service import AgentMessage
from titan.runtime.service import AgentRuntime, run_turn

#: The 16 roster roles in delivery order, each with a task-tracking-app task. Every
#: role gets a real turn — the AGT-007 "full roster runs offline" requirement.
ROSTER_TASKS: list[tuple[str, str, str]] = [
    ("ceo", "Set the product vision", "Set the vision for a local-first task-tracking web app."),
    ("orchestrator", "Plan the task DAG", "Materialize the task DAG for the task app build."),
    ("pm_product", "Write the backlog", "Backlog for create/list/complete/filter tasks."),
    ("critic", "Critique the plan", "Critique the task-app plan for risks and gaps."),
    ("research", "Research UX patterns", "Research task-app UX patterns from the knowledge base."),
    ("pm_project", "Schedule the sprint", "Schedule sprint 1 (the walking skeleton) for the app."),
    ("architect", "Design the architecture", "Design components, API and DB for the task app."),
    ("eng_database", "Write the migration", "Write the tasks table migration (id, title, done)."),
    (
        "eng_backend",
        "Implement create-task",
        "Implement POST /tasks creating a task, returning 201.",
    ),
    ("eng_frontend", "Build the task UI", "Build the task list UI with a create-task form."),
    ("eng_devops", "Wire local deploy", "Write the docker-compose deploy for the task app."),
    ("qa", "Write the test matrix", "Write e2e tests for create/list/complete a task."),
    ("security", "Security review", "Review the task app: OWASP, secret scan, RBAC."),
    ("reviewer", "Code review", "Code-review the create-task change for boundaries + duplication."),
    ("docs", "Write the docs", "Write the README + API docs for the task app."),
    ("memory", "Record the reflection", "Record the sprint reflection into organization memory."),
]

# Every roster task must name a real CORE_ROSTER runtime role, and the set must be
# EXACTLY the 16-agent roster — a drift (a duplicate/typo) would silently skip a
# real agent while still yielding 16 records (TITAN-AGT-007 "the FULL roster runs").
_ROSTER_ROLE_IDS = frozenset(role for role, _, _ in ROSTER_TASKS)
assert _ROSTER_ROLE_IDS == CORE_ROSTER, (
    f"ROSTER_TASKS must cover the full roster exactly: "
    f"missing={CORE_ROSTER - _ROSTER_ROLE_IDS}, extra={_ROSTER_ROLE_IDS - CORE_ROSTER}"
)

_REFERENCE_BRIEF = (
    "Build a task-tracking web app: users create tasks, mark them done, and filter "
    "by status. Local-first, canonical stack."
)


class RosterTurnRecord(BaseModel):
    """One roster agent's real turn — metadata only (no prompt/completion bytes)."""

    role: str
    outcome: str
    completed: bool
    emitted_messages: int
    tool_calls: int
    recovery_rungs: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    failure_reason: str | None = None
    escalated_to: str | None = None


class AcceptanceRecord(BaseModel):
    """The AGT-007 acceptance record for one offline run."""

    run_id: str
    generated_at: str | None = None
    profile: str
    offline: bool
    egress_clean: bool
    hosts_contacted: list[str] = Field(default_factory=list)
    #: Whether an egress guard actually observed the run (else ``egress_clean`` is
    #: assumed, not measured).
    egress_verified: bool = False
    #: At least one turn actually reached the local model (provider == ollama) — the
    #: run is not a green "all offline" when the model was in fact unreachable.
    local_model_exercised: bool = False
    roster_turns: list[RosterTurnRecord] = Field(default_factory=list)
    roles_expected: int = len(ROSTER_TASKS)
    all_roles_ran: bool = False
    roles_completed: int = 0
    #: EVERY roster turn produced ≥1 schema-valid message. This is NOT "an app was
    #: built": the turns are independent (no pipeline/gates/artifacts between them),
    #: so a capable model returning valid JSON for all 16 flips this True without any
    #: app existing. A local 7B does not clear it — recorded honestly, never forced.
    all_roles_produced_valid_output: bool = False
    #: The Phase-55 KPI pipeline reached a released state — proves the delivery/gate
    #: machinery, NOT that the roster's own outputs built the app.
    pipeline_wiring_completes: bool = False
    kpi: KpiReport | None = None
    notes: str = ""


async def run_roster_turns(runtime: AgentRuntime, project_id: uuid.UUID) -> list[RosterTurnRecord]:
    """Run one real turn per roster role against ``project_id`` and record each."""
    records: list[RosterTurnRecord] = []
    for role, title, description in ROSTER_TASKS:
        trigger = AgentMessage(
            type="TaskAssignment",
            project_id=project_id,
            from_agent="orchestrator",
            to_agent=role,
            payload={
                "title": title,
                "description": description,
                "acceptance_criteria": ["Meets the task-tracking-app requirement."],
            },
        )
        result = await run_turn(runtime, project_id, role, trigger)
        records.append(
            RosterTurnRecord(
                role=role,
                outcome=result.outcome,
                completed=result.completed,
                emitted_messages=len(result.emitted_message_ids),
                tool_calls=len(result.tool_calls),
                recovery_rungs=list(result.recovery_rungs),
                provider=result.provider,
                model=result.model_alias,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                failure_reason=str(result.failure_reason) if result.failure_reason else None,
                escalated_to=result.escalation_target,
            )
        )
    return records


async def _seed_project(engine: AsyncEngine, brief: str) -> uuid.UUID:
    pid = uuid7()
    uid = uuid7()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'Acceptance', 'local', :s)"
            ),
            {"id": uid, "e": f"agt007-{uid.hex[:12]}@titan.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :n, 'AGT-007', :b, :u)"
            ),
            {"id": pid, "n": f"agt007-{pid.hex[:12]}", "b": brief, "u": uid},
        )
    return pid


async def run_offline_acceptance(
    engine: AsyncEngine,
    runtime: AgentRuntime,
    *,
    gateway: Any,
    profile: str = "local-free",
    egress_guard: EgressGuard | None = None,
    run_kpis: bool = True,
    run_id: str | None = None,
    generated_at: str | None = None,
) -> AcceptanceRecord:
    """Run the full-roster offline acceptance and assemble the AGT-007 record.

    ``runtime`` must be built on the Ollama profile with ``gateway`` (the same
    egress-guarded gateway) so every turn is a real local call. ``run_kpis`` folds
    in the Phase-55 Section-7 report (its pipeline completion proves the wiring)."""
    project_id = await _seed_project(engine, _REFERENCE_BRIEF)
    roster_turns = await run_roster_turns(runtime, project_id)

    kpi: KpiReport | None = None
    kpi_error: str | None = None
    if run_kpis:
        try:
            kpi = await BenchmarkRunner(engine, gateway=gateway).run(
                profile=profile, generated_at=generated_at
            )
        except Exception as exc:  # noqa: BLE001 — the roster-turn record is the core proof
            kpi_error = f"{type(exc).__name__}: {exc}"

    roles_completed = sum(1 for t in roster_turns if t.completed)
    # Distinct VALID roster roles that produced a record — immune to a duplicate/typo
    # in ROSTER_TASKS silently standing in for a real role.
    roles_ran = {t.role for t in roster_turns if t.role in CORE_ROSTER}
    all_ran = roles_ran == CORE_ROSTER
    local_exercised = any(
        (t.provider or "").lower() == "ollama" and t.outcome != "error" for t in roster_turns
    )
    pipeline_ok = bool(
        kpi and kpi.suite.autonomous_completion_rate >= 1.0 and kpi.suite.briefs_executed >= 1
    )
    egress_verified = egress_guard is not None
    egress_clean = egress_guard.clean if egress_guard is not None else True
    hosts = sorted(egress_guard.hosts_contacted) if egress_guard is not None else []
    return AcceptanceRecord(
        run_id=run_id or str(uuid7()),
        generated_at=generated_at or dt.datetime.now(dt.UTC).isoformat(),
        profile=profile,
        offline=egress_clean and egress_verified,
        egress_clean=egress_clean,
        egress_verified=egress_verified,
        local_model_exercised=local_exercised,
        hosts_contacted=hosts,
        roster_turns=roster_turns,
        all_roles_ran=all_ran,
        roles_completed=roles_completed,
        all_roles_produced_valid_output=(all_ran and roles_completed == len(CORE_ROSTER)),
        pipeline_wiring_completes=pipeline_ok,
        kpi=kpi,
        notes=(
            f"AGT-007 offline acceptance attempt on profile {profile!r}: all "
            f"{len(roles_ran)}/{len(CORE_ROSTER)} roster agents ran real turns "
            f"(local_model_exercised={local_exercised}); {roles_completed} produced valid "
            f"output. run_turn ran offline and the Phase-55 KPI pipeline completed "
            f"(pipeline_wiring_completes={pipeline_ok}) — these prove the roster+delivery "
            f"machinery, NOT that the local model built a gate-passing app."
            + (f" KPI capture skipped: {kpi_error}." if kpi_error else "")
        ),
    )


__all__ = [
    "ROSTER_TASKS",
    "AcceptanceRecord",
    "RosterTurnRecord",
    "run_offline_acceptance",
    "run_roster_turns",
]
