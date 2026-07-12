"""Public service interface of the ``loop`` module (Vol 6 §3, TITAN-WFL-010–014).

``run_loop`` drives one task through the bounded self-improvement loop: it enters
IN_LOOP, runs ≤ ``max_cycles`` review→refactor→test→scan→perf cycles, and ends by
converging (→ MERGE) or escalating (→ ESCALATED + Architect arbitration via the
Phase-31 conflict machine). ``assert_loop_approved`` is the TITAN-WFL-010 gate the
task machine calls: no MERGE without a converged loop record.

The driver (the LLM-backed engineer/reviewer/QA actors) is called OUTSIDE any DB
transaction — each cycle gathers all actor outputs, then persists them atomically —
so a slow actor never holds a database lock. Reviewer isolation (TITAN-WFL-011) is
structural: the reviewer only ever receives a :class:`ReviewerContext`.

Only this interface may be imported by other modules. Pydantic v2 DTOs cross the
boundary.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.conflict import service as conflict
from titan.constitution import service as constitution
from titan.db import uuid7
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import not_found
from titan.loop import engine
from titan.loop import repository as repo
from titan.loop.engine import (
    CONVERGENCE_STREAK,
    REFUTE_UPHELD_ARBITRATION_THRESHOLD,
    CycleTally,
    Finding,
    LoopDriver,
    OscillationTracker,
    ReviewerContext,
    is_minor_plus,
    next_consecutive_clean,
)
from titan.orchestrator import service as orchestrator
from titan.settings import get_settings

# Which §3.2 actor owns each non-review stage (for the audit ledger).
_STAGE_ACTOR = {"testing": "qa", "security_scan": "security", "perf_analysis": "perf"}


def _clamp_cycles(n: int) -> int:
    """Clamp the loop cap to the configurable 2–5 range (TITAN-WFL-013)."""
    return max(2, min(5, n))


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type=problem_type("conflict")
    )


# --- DTOs -------------------------------------------------------------------


class LoopOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: uuid.UUID
    loop_id: uuid.UUID
    #: running | converged | escalated | arbitration
    status: str
    #: converged | exhausted (None unless terminal)
    termination_reason: str | None
    cycles: int
    #: The task's status after the loop's terminal move (merged | escalated | in_loop).
    task_status: str


class LoopRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    status: str
    iteration: int
    cycle_count: int
    max_cycles: int
    termination_reason: str | None
    created_at: datetime
    updated_at: datetime


class LoopCycleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cycle_number: int
    new_findings_count: int
    clean: bool
    outcome: str
    created_at: datetime


class LoopFindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    raised_cycle: int
    severity: str
    file: str
    line: int
    rationale: str
    suggestion: str
    fingerprint: str
    rule_code: str | None
    advisory: bool
    disposition: str
    refutation_reason: str | None
    refute_upheld_count: int
    conflict_id: uuid.UUID | None


class LoopStageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cycle_number: int
    stage: str
    actor: str
    verdict: str
    detail: dict[str, Any]
    at: datetime


class LoopView(BaseModel):
    record: LoopRecordOut
    cycles: list[LoopCycleOut]
    findings: list[LoopFindingOut]
    stage_events: list[LoopStageEventOut]


def _outcome(row: Row[Any], task_status: str) -> LoopOutcome:
    return LoopOutcome(
        task_id=row.task_id,
        loop_id=row.id,
        status=row.status,
        termination_reason=row.termination_reason,
        cycles=row.cycle_count,
        task_status=task_status,
    )


# --- the TITAN-WFL-010 merge gate -------------------------------------------


async def assert_loop_approved(conn: AsyncConnection, task_id: uuid.UUID) -> None:
    """The task machine calls this before a ``merge`` (in_loop → merged): a task
    SHALL NOT reach MERGED without a converged self-improvement loop record for its
    CURRENT work (TITAN-WFL-010 — no first drafts). Fail-closed: a missing,
    non-converged, or superseded (reopened) latest loop blocks the merge."""
    loop = await repo.latest_loop(conn, task_id)
    if loop is None or loop.status != "converged":
        raise _conflict(
            "Cannot merge: the task has no converged self-improvement loop record "
            "(TITAN-WFL-010). A component ships only after its loop converges."
        )


async def supersede_loop_on_reopen(conn: AsyncConnection, task_id: uuid.UUID) -> None:
    """Task-machine hook (on ``reopen``): retire a converged loop so the re-worked
    task must run — and converge — a fresh loop before it can merge again."""
    await repo.supersede_converged_loop(conn, task_id)


# --- the engine driver ------------------------------------------------------


async def _open_arbitration(
    conn: AsyncConnection, project_id: uuid.UUID, *, subject: str, actor: str
) -> uuid.UUID:
    """Route a dispute to the Architect through the Phase-31 conflict machine
    (never a second ladder). A ``technical`` dispute is arbitrated by the Architect
    (mechanical appointment)."""
    conflict_id = uuid7()
    await conflict.open_conflict(
        conn,
        project_id,
        conflict_id=conflict_id,
        kind="technical",
        subject=subject,
        parties=["reviewer", "engineer"],
        opened_by=actor,
    )
    await conflict.escalate_to_arbitration(conn, project_id, conflict_id, actor=actor)
    return conflict_id


async def run_loop(
    db: AsyncEngine,
    task_id: uuid.UUID,
    driver: LoopDriver,
    *,
    actor_user_id: uuid.UUID,
    pr_diff: str,
    task_spec: str,
    architecture_excerpt: str,
    max_cycles: int | None = None,
) -> LoopOutcome:
    """Run the self-improvement loop for one task (§3). Idempotent: re-invoking on
    an already-terminal loop returns its outcome."""
    cap = _clamp_cycles(max_cycles if max_cycles is not None else get_settings().loop_max_cycles)

    # --- setup: pick/open the loop iteration, enter IN_LOOP, build reviewer ctx --
    async with db.begin() as conn:
        task = await orchestrator.get_task(conn, task_id)
        project_id = task.project_id
        latest = await repo.latest_loop(conn, task_id)
        # Idempotent no-ops (re-invocation): an arbitration is paused for the
        # Architect; a converged/escalated loop that already drove the task to its
        # terminal state (and was NOT reopened) is done. A reopened task (converged
        # loop but task back in progress) instead runs a fresh iteration below.
        if latest is not None and (
            latest.status == "arbitration"
            or (latest.status == "converged" and task.status in ("merged", "done"))
            or (latest.status == "escalated" and task.status == "escalated")
        ):
            return _outcome(latest, task.status)
        if task.status == "in_progress":
            enter = orchestrator.transitions.resolve("in_progress", "enter_loop")
            assert enter is not None
            task = await orchestrator.apply_transition(
                conn, task, enter, actor_agent_id=None, actor_user_id=actor_user_id, comment=None
            )
        elif task.status != "in_loop":
            raise _conflict(
                f"the self-improvement loop runs only from in_progress/in_loop; "
                f"task is {task.status}."
            )
        if latest is not None and latest.status == "running":
            loop_row = latest  # resume a partially-run loop after a crash
        else:
            iteration = (latest.iteration + 1) if latest is not None else 1
            loop_row = await repo.create_loop_record(
                conn, task_id, project_id, iteration=iteration, max_cycles=cap
            )
        loop_id = loop_row.id
        # Resume from persisted state (TITAN-WFL-013 survives a mid-loop crash): the
        # next cycle is cycle_count+1 and the convergence streak is the tail of clean
        # cycles already recorded.
        resume_cycle = int(loop_row.cycle_count)
        consecutive_clean = await repo.trailing_clean_count(conn, loop_id) if resume_cycle else 0
        active = await constitution.active_constitution(conn)
    reviewer_ctx = ReviewerContext(
        pr_diff=pr_diff,
        task_spec=task_spec,
        architecture_excerpt=architecture_excerpt,
        constitution_version=active.version,
        constitution_body=active.body,
    )

    oscillation = OscillationTracker()
    outcome: str | None = None
    cycle = resume_cycle

    while cycle < cap:
        cycle += 1
        # ---- gather all actor outputs for the cycle (NO DB transaction held) ----
        findings = list(await driver.review(reviewer_ctx, cycle))
        gathered: list[tuple[Finding, engine.Disposition, bool | None, bool]] = []
        for finding in findings:
            # Advisory findings (outside the diff's blast radius, TITAN-WFL-033)
            # NEVER block: they skip refute-or-fix and arbitration entirely and
            # take a terminal, non-disputed disposition. Routing one to the
            # engineer or the Architect would let an out-of-scope note block merge.
            if finding.advisory:
                gathered.append(
                    (
                        finding,
                        engine.Disposition(
                            kind="refuted", reason="advisory: outside the diff's blast radius"
                        ),
                        None,
                        False,
                    )
                )
                continue
            disp = await driver.dispose(finding, cycle)
            oscillated = False
            if disp.new_hunk_state is not None:
                oscillated = oscillation.record(
                    finding.hunk_key or finding.fingerprint, disp.new_hunk_state
                )
            upheld: bool | None = None
            if disp.kind == "refuted" and not oscillated:
                upheld = await driver.rejudge(finding, disp, cycle)
            gathered.append((finding, disp, upheld, oscillated))
        # An oscillation short-circuits to arbitration, so the QA/security/perf
        # stages are skipped this cycle (a refute-count arbitration is only known
        # once persisted, and simply drops the gathered stage results).
        any_oscillation = any(osc for *_, osc in gathered)
        stage_results: dict[str, tuple[bool, str]] = {}
        if not any_oscillation:
            for stage in ("testing", "security_scan", "perf_analysis"):
                stage_results[stage] = await driver.stage_report(stage, cycle)

        # ---- persist the whole cycle atomically ----
        async with db.begin() as conn:
            await repo.insert_stage_event(
                conn,
                loop_id=loop_id,
                task_id=task_id,
                cycle_number=cycle,
                stage="review",
                actor="reviewer",
                verdict="n/a",
                # Isolated-context manifest (TITAN-WFL-011): which sections the
                # reviewer received — NEVER the implementer's chain-of-thought.
                detail={
                    "context_sections": reviewer_ctx.sections(),
                    "constitution_version": reviewer_ctx.constitution_version,
                    "excludes_implementer_context": True,
                    "findings": len(findings),
                },
            )
            new_minor_plus = 0
            for finding in findings:
                is_new = await repo.insert_finding_if_new(
                    conn,
                    loop_id=loop_id,
                    task_id=task_id,
                    raised_cycle=cycle,
                    severity=finding.severity,
                    file=finding.file,
                    line=finding.line,
                    rationale=finding.rationale,
                    suggestion=finding.suggestion,
                    fingerprint=finding.fingerprint,
                    hunk_key=finding.hunk_key,
                    rule_code=finding.rule_code,
                    advisory=finding.advisory,
                )
                # Advisory findings (out of blast radius, TITAN-WFL-033) are
                # recorded but never count toward convergence — they cannot block.
                if is_new and is_minor_plus(finding) and not finding.advisory:
                    new_minor_plus += 1

            # Every finding that reaches arbitration this cycle (oscillation OR a
            # second upheld refutation) is routed — never just the last one.
            arb_findings: dict[str, Finding] = {}
            for finding, disp, upheld, oscillated in gathered:
                await repo.insert_stage_event(
                    conn,
                    loop_id=loop_id,
                    task_id=task_id,
                    cycle_number=cycle,
                    stage="refactor",
                    actor="engineer",
                    verdict=disp.kind,
                    detail={"fingerprint": finding.fingerprint, "kind": disp.kind},
                )
                if oscillated:
                    arb_findings[finding.fingerprint] = finding
                    continue
                if disp.kind == "fixed":
                    await repo.set_finding_disposition(
                        conn, loop_id=loop_id, fingerprint=finding.fingerprint, disposition="fixed"
                    )
                elif upheld:
                    count = await repo.bump_refute_upheld(
                        conn, loop_id=loop_id, fingerprint=finding.fingerprint, reason=disp.reason
                    )
                    await repo.insert_stage_event(
                        conn,
                        loop_id=loop_id,
                        task_id=task_id,
                        cycle_number=cycle,
                        stage="critique",
                        actor="reviewer",
                        verdict="upheld",
                        detail={"fingerprint": finding.fingerprint, "refute_upheld_count": count},
                    )
                    if count >= REFUTE_UPHELD_ARBITRATION_THRESHOLD:
                        arb_findings[finding.fingerprint] = finding
                else:
                    await repo.set_finding_disposition(
                        conn,
                        loop_id=loop_id,
                        fingerprint=finding.fingerprint,
                        disposition="refuted",
                        refutation_reason=disp.reason,
                    )

            if arb_findings:
                for finding in arb_findings.values():
                    conflict_id = await _open_arbitration(
                        conn,
                        project_id,
                        subject=f"loop finding {finding.fingerprint} on task {task_id}",
                        actor="orchestrator",
                    )
                    await repo.set_finding_arbitration(
                        conn,
                        loop_id=loop_id,
                        fingerprint=finding.fingerprint,
                        conflict_id=conflict_id,
                    )
                await repo.insert_cycle(
                    conn,
                    loop_id=loop_id,
                    cycle_number=cycle,
                    new_findings_count=new_minor_plus,
                    clean=False,
                    outcome="arbitration",
                )
                await repo.set_loop_status(conn, loop_id, status="arbitration", cycle_count=cycle)
                outcome = "arbitration"
            else:
                for stage, (ok, note) in stage_results.items():
                    await repo.insert_stage_event(
                        conn,
                        loop_id=loop_id,
                        task_id=task_id,
                        cycle_number=cycle,
                        stage=stage,
                        actor=_STAGE_ACTOR[stage],
                        verdict="pass" if ok else "fail",
                        detail={"note": note},
                    )
                open_mp = await repo.open_minor_plus_count(conn, loop_id)
                tally = CycleTally(new_minor_plus=new_minor_plus, open_minor_plus=open_mp)
                consecutive_clean = next_consecutive_clean(consecutive_clean, tally)
                converged = consecutive_clean >= CONVERGENCE_STREAK
                await repo.insert_cycle(
                    conn,
                    loop_id=loop_id,
                    cycle_number=cycle,
                    new_findings_count=new_minor_plus,
                    clean=tally.clean,
                    outcome="converged" if converged else "continue",
                )
                await repo.set_cycle_count(conn, loop_id, cycle)
                if converged:
                    outcome = "converged"

        if outcome is not None:
            break

    if outcome is None:
        outcome = "exhausted"

    # --- terminal: drive the task machine + (for exhaustion) the conflict ladder -
    async with db.begin() as conn:
        task = await orchestrator.get_task(conn, task_id)
        if outcome == "converged":
            await repo.set_loop_status(
                conn, loop_id, status="converged", termination_reason="converged", cycle_count=cycle
            )
            await repo.insert_stage_event(
                conn,
                loop_id=loop_id,
                task_id=task_id,
                cycle_number=cycle,
                stage="approval",
                actor="reviewer",
                verdict="pass",
                detail={"reason": "converged"},
            )
            merge = orchestrator.transitions.resolve("in_loop", "merge")
            assert merge is not None
            task = await orchestrator.apply_transition(
                conn, task, merge, actor_agent_id=None, actor_user_id=actor_user_id, comment=None
            )
            await repo.insert_stage_event(
                conn,
                loop_id=loop_id,
                task_id=task_id,
                cycle_number=cycle,
                stage="merge",
                actor="devops",
                verdict="pass",
                detail={},
            )
        elif outcome == "exhausted":
            await repo.set_loop_status(
                conn, loop_id, status="escalated", termination_reason="exhausted", cycle_count=cycle
            )
            await _open_arbitration(
                conn,
                task.project_id,
                subject=f"self-improvement loop exhausted ({cycle} cycles) on task {task_id}",
                actor="orchestrator",
            )
            escalate = orchestrator.transitions.resolve("in_loop", "escalate")
            assert escalate is not None
            task = await orchestrator.apply_transition(
                conn, task, escalate, actor_agent_id=None, actor_user_id=actor_user_id, comment=None
            )
        # arbitration: the task stays IN_LOOP awaiting the Architect's verdict; the
        # conflict was already opened during the cycle.
        final = await repo.latest_loop(conn, task_id)
        assert final is not None
    return _outcome(final, task.status)


# --- reads ------------------------------------------------------------------


async def get_loop(conn: AsyncConnection, task_id: uuid.UUID) -> LoopView:
    record = await repo.latest_loop(conn, task_id)
    if record is None:
        raise not_found("loop record")
    cycles = await repo.cycles_for_loop(conn, record.id)
    findings = await repo.findings_for_loop(conn, record.id)
    events = await repo.stage_events_for_loop(conn, record.id)
    return LoopView(
        record=LoopRecordOut.model_validate(record),
        cycles=[LoopCycleOut.model_validate(c) for c in cycles],
        findings=[LoopFindingOut.model_validate(f) for f in findings],
        stage_events=[LoopStageEventOut.model_validate(e) for e in events],
    )


__all__ = [
    "Finding",
    "LoopCycleOut",
    "LoopDriver",
    "LoopFindingOut",
    "LoopOutcome",
    "LoopRecordOut",
    "LoopStageEventOut",
    "LoopView",
    "ReviewerContext",
    "assert_loop_approved",
    "get_loop",
    "run_loop",
    "supersede_loop_on_reopen",
]
