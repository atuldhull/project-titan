"""Public service interface of the `handoffs` module (TITAN-BCK-004).

Responsibility: the generic §2.2 handoff executor + the TITAN-WFL-004 gate ledger.
A handoff IS a Phase-41 pipeline stage transition: the producing stage submits its
§2.3 artifact, the executor validates it against the versioned schema
(TITAN-WFL-003 — a validation failure IS a gate failure with a field-level report),
evaluates the acceptance gate, records the evaluation in the append-only ledger,
and then advances / retries / escalates the pipeline within the TITAN-WFL-005 retry
budget. On budget exhaustion the pipeline pauses at NEEDS_HUMAN with exactly one
structured escalation and no further work for that stage.

Only this interface may be imported by other modules; the models, repository,
gates, and router stay private. Pydantic v2 DTOs — never ORM rows — cross the
boundary.
"""

import hashlib
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.constitution import service as constitution
from titan.debt import service as debt
from titan.events.service import STREAM_EVENTS_DOMAIN, write_event
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import not_found
from titan.handoffs import gates
from titan.handoffs import repository as repo
from titan.handoffs.gates import Gate, GateContext, GateResult, register_gate, validate_schema
from titan.handoffs.release_deploy import DeployGateOutcome, run_deploy_gate
from titan.handoffs.schemas import ALL_ARTIFACT_SCHEMAS, ARTIFACT_SCHEMAS, schema_for_stage
from titan.handoffs.standard_gates import evaluate_intake_gate, register_standard_gates
from titan.handoffs.standard_gates_release import medium_security_findings, review_comments
from titan.handoffs.traceability import Traceability, build_traceability
from titan.llm import service as llm
from titan.messaging import service as messaging
from titan.orchestrator import service as orchestrator
from titan.projects import service as projects
from titan.settings import get_settings

# Re-export the stage → schema mapping + registry hooks other modules/tests use.
PRODUCING_STAGES: tuple[str, ...] = tuple(ARTIFACT_SCHEMAS)

Outcome = Literal["advanced", "gate_failed_retry", "escalated", "schema_rejected"]


# --- DTOs -------------------------------------------------------------------


class HandoffOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    stage: str
    #: The stage advanced to (only when ``outcome == "advanced"``).
    next_stage: str | None = None
    #: 1-based count of this stage's gate FAILURES so far (0 on a pass).
    attempt: int
    #: Machine-checkable gate reasons (empty on a pass).
    gate_reasons: list[str] = []
    #: Field-level schema errors (only when ``outcome == "schema_rejected"``).
    field_errors: list[str] = []
    #: The stored artifact's version (None when schema validation rejected it).
    artifact_version: int | None = None
    pipeline: orchestrator.PipelineOut


class GateEvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    stage: str
    artifact_id: uuid.UUID | None
    attempt: int
    verdict: str
    reasons: list[Any]
    evaluator_agent: str
    evaluator_model: str
    prompt_version: str
    #: The constitution version this gate judged against (G5 only; None otherwise).
    constitution_version: int | None = None
    created_at: datetime


class GateLedger(BaseModel):
    project_id: uuid.UUID
    evaluations: list[GateEvaluationOut]


class HandoffArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    stage: str
    artifact_type: str
    schema_version: int
    version: int
    body: dict[str, Any]
    submitted_by: str
    created_at: datetime


class ArtifactVersion(BaseModel):
    """One revision in a stage's history — metadata only, no ``body``."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    stage: str
    artifact_type: str
    schema_version: int
    version: int
    submitted_by: str
    created_at: datetime


class ArtifactVersions(BaseModel):
    """A stage's revision history, newest first.

    Not a paginated `Page`: this is a bounded per-(project, stage) history, and a
    timeline that renders "v1..vN" needs all of N to draw itself. It stays small
    because a version is an agent RESUBMISSION — one per gate-failure retry, not
    per edit."""

    project_id: uuid.UUID
    stage: str
    versions: list[ArtifactVersion]


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type=problem_type("conflict")
    )


def _bad_stage(stage: str) -> ProblemException:
    return ProblemException(
        status=422,
        title="Invalid handoff stage",
        detail=(
            f"'{stage}' is not a producing pipeline stage with an artifact schema. "
            f"Producing stages: {', '.join(PRODUCING_STAGES)}."
        ),
        problem_type=problem_type("validation"),
    )


async def _emit_gate_event(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    stage: str,
    verdict: str,
    reasons: list[str],
    attempt: int,
    actor: str,
) -> None:
    """Emit the §2.2 handoff outcome (the "failure event" a field-level report
    attaches to) through the Phase-10 outbox → project WS channel. Carries only
    metadata (stage, verdict, reason strings, attempt) — never artifact content
    (TITAN-OPS-016)."""
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {
            "type": "handoff.evaluated",
            "handoff": {
                "project_id": str(project_id),
                "stage": stage,
                "verdict": verdict,
                "reasons": reasons,
                "attempt": attempt,
            },
        },
        actor=actor,
    )


async def _build_gate_context(
    conn: AsyncConnection, project_id: uuid.UUID, stage: str, body: dict[str, Any]
) -> GateContext:
    """Assemble the pure gate's :class:`GateContext` — the only DB access a gate's
    evaluation performs. Loads each earlier producing stage's latest artifact (for
    G3/G4/G5 cross-artifact checks) and, for the design gate, the active
    constitution (retrieved into context; its version is stamped on the G5 ledger
    row). The gate predicate itself stays pure and side-effect-free."""
    priors: dict[str, dict[str, Any]] = {}
    for prior in PRODUCING_STAGES:
        if prior == stage:
            break
        row = await repo.latest_artifact(conn, project_id, prior)
        if row is not None:
            priors[prior] = dict(row.body)
    constitution_version: int | None = None
    constitution_body = ""
    if stage == "architect_design":
        active = await constitution.active_constitution(conn)
        constitution_version = active.version
        constitution_body = active.body
    return GateContext(
        stage=stage,
        body=body,
        prior_artifacts=priors,
        constitution_version=constitution_version,
        constitution_body=constitution_body,
        sprint_capacity_days=get_settings().sprint_capacity_days,
        project_id=project_id,
    )


def _debt_fingerprint(source: str, content: str) -> str:
    """A stable dedup handle for a debt ticket so a re-run gate reuses its ticket."""
    return f"{source}:{hashlib.sha256(content.encode()).hexdigest()[:16]}"


async def _open_debt_after_pass(
    conn: AsyncConnection, project_id: uuid.UUID, stage: str, body: dict[str, Any]
) -> None:
    """TITAN-WFL-021: a passing gate that ACCEPTS a constitution deviation opens a
    debt ticket with a proposed paydown. G9 APPROVED_WITH_COMMENTS logs each review
    comment; G8 logs each accepted medium-severity security finding. Idempotent by
    fingerprint, so re-submitting the same artifact never duplicates a ticket."""
    if stage == "code_review" and str(body.get("verdict")) == "APPROVED_WITH_COMMENTS":
        # WFL-021: EVERY APPROVED_WITH_COMMENTS verdict opens a ticket — even one that
        # carried no itemized comments, so the invariant holds per verdict, not per
        # comment.
        comments = list(review_comments(body)) or [
            "release approved with comments (no itemized comments recorded)"
        ]
        for comment in comments:
            await debt.open_ticket(
                conn,
                project_id,
                source="code_review",
                title="Review comment accepted at release (APPROVED_WITH_COMMENTS)",
                detail=comment,
                fingerprint=_debt_fingerprint("code_review", comment),
                paydown_proposal=f"Address the accepted review comment: {comment}",
            )
    elif stage == "security":
        for finding in medium_security_findings(body):
            await debt.open_ticket(
                conn,
                project_id,
                source="security",
                title="Accepted medium-severity security finding",
                detail=finding,
                fingerprint=_debt_fingerprint("security", finding),
                paydown_proposal=f"Remediate the accepted medium finding: {finding}",
            )


async def submit_artifact(
    engine: AsyncEngine,
    project_id: uuid.UUID,
    stage: str,
    body: dict[str, Any],
    *,
    submitted_by: str,
) -> HandoffOutcome:
    """Execute one handoff (§2.2). Owns its transaction so the artifact store, the
    gate-ledger row, and the pipeline advance/retry/escalate all commit atomically.

    Rejects (422) an unknown stage; (409) a stage that is not the pipeline's
    current stage, a non-running pipeline, or a budget-held project (no gate is
    evaluated then, so no LLM is spent). Otherwise validates the schema, stores the
    artifact, evaluates the gate, records the evaluation, and drives the pipeline.
    """
    if schema_for_stage(stage) is None:
        raise _bad_stage(stage)

    async with engine.begin() as conn:
        # Serialize the whole handoff per project (FOR UPDATE on the pipeline row):
        # the retry budget + escalation must be exactly-once and no concurrent
        # submission may skip a stage's gate or collide on an artifact version.
        pipe = await orchestrator.ensure_and_lock_pipeline(conn, project_id, actor=submitted_by)
        if pipe.status != "running":
            raise _conflict(
                f"Pipeline is {pipe.status}, not running — no handoff can be processed "
                "(raise the budget or resolve the escalation first)."
            )
        if stage != pipe.current_stage:
            raise _conflict(
                f"Cannot submit a '{stage}' artifact: the pipeline is at '{pipe.current_stage}'. "
                "Handoffs are the pipeline's own stage transitions and cannot skip ahead."
            )
        # A budget hold pauses the handoff BEFORE the gate — the gate is where a
        # real evaluator would spend LLM (TITAN-WFL-005 "zero further LLM calls").
        held = await llm.active_budget_hold(conn, project_id=project_id, task_id=None)
        if held is not None:
            raise _conflict(f"Pipeline paused on budget hold: {held}. Raise the budget to resume.")

        # 1) Schema gate (TITAN-WFL-003): a validation failure IS a gate failure.
        schema_result, normalized = validate_schema(stage, body)
        if not schema_result.passed or normalized is None:
            outcome = await _record_and_route(
                conn,
                project_id=project_id,
                stage=stage,
                artifact_id=None,
                result=GateResult(
                    passed=False,
                    reasons=schema_result.reasons,
                    evaluator_agent="schema-validator",
                    evaluator_model="pydantic-v2",
                    prompt_version=f"schema-v{_schema_version(stage)}",
                ),
                pipe=pipe,
                submitted_by=submitted_by,
                schema_rejected=True,
                field_errors=schema_result.reasons,
            )
            return outcome

        # 2) Store the validated, canonicalized artifact (a new version).
        version = await repo.next_artifact_version(conn, project_id, stage)
        artifact_row = await repo.insert_artifact(
            conn,
            project_id=project_id,
            stage=stage,
            artifact_type=str(normalized.get("artifact_type", stage)),
            schema_version=int(normalized.get("schema_version", 1)),
            version=version,
            body=normalized,
            submitted_by=submitted_by,
        )
        # (The artifact is durable in Postgres; embedding into the artifact-retrieval
        # Qdrant collection is wired when artifact retrieval lands — the store is the
        # source of truth, retrieval is additive.)

        # 3) Acceptance gate (TITAN-WFL-004). G2–G5 are registered concrete
        # predicates (standard_gates); unregistered stages auto-pass a schema-valid
        # artifact. The gate is pure — the executor assembles its context here.
        ctx = await _build_gate_context(conn, project_id, stage, normalized)
        gate_result = gates.gate_for_stage(stage).evaluate(ctx)
        outcome = await _record_and_route(
            conn,
            project_id=project_id,
            stage=stage,
            artifact_id=artifact_row.id,
            result=gate_result,
            pipe=pipe,
            submitted_by=submitted_by,
            schema_rejected=False,
            artifact_version=version,
        )
        # WFL-021: a gate that PASSED while accepting a deviation (G9 ACWC / G8
        # mediums) records the debt in the same transaction as the advance.
        if outcome.outcome == "advanced":
            await _open_debt_after_pass(conn, project_id, stage, normalized)
        return outcome


async def _record_and_route(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    stage: str,
    artifact_id: uuid.UUID | None,
    result: GateResult,
    pipe: orchestrator.PipelineOut,
    submitted_by: str,
    schema_rejected: bool,
    field_errors: list[str] | None = None,
    artifact_version: int | None = None,
) -> HandoffOutcome:
    """Persist the gate evaluation, emit the outcome event, then drive the pipeline:
    PASS → advance; FAIL within budget → retry; FAIL past budget → escalate."""
    verdict = "pass" if result.passed else "fail"
    # attempt = the failure count INCLUDING this one (0 for a pass).
    prior_failures = await repo.failed_gate_count(conn, project_id, stage)
    attempt = prior_failures + 1 if not result.passed else 0
    await repo.insert_gate_evaluation(
        conn,
        project_id=project_id,
        stage=stage,
        artifact_id=artifact_id,
        attempt=attempt,
        verdict=verdict,
        reasons=result.reasons,
        evaluator_agent=result.evaluator_agent,
        evaluator_model=result.evaluator_model,
        prompt_version=result.prompt_version,
        constitution_version=result.constitution_version,
    )
    await _emit_gate_event(
        conn,
        project_id=project_id,
        stage=stage,
        verdict=verdict,
        reasons=result.reasons,
        attempt=attempt,
        actor=submitted_by,
    )

    if result.passed:
        advanced = await orchestrator.advance_stage(
            conn, project_id, actor=submitted_by, detail={"gate": "passed", "stage": stage}
        )
        return HandoffOutcome(
            outcome="advanced",
            stage=stage,
            next_stage=advanced.current_stage,
            attempt=0,
            artifact_version=artifact_version,
            pipeline=advanced,
        )

    # Failure path (TITAN-WFL-005). ``attempt`` failures have now occurred; escalate
    # once the budget is exhausted (the (budget+1)-th failure), else retry.
    if attempt > pipe.retry_budget:
        updated = await _escalate(conn, project_id, stage=stage, actor=submitted_by)
        return HandoffOutcome(
            outcome="escalated",
            stage=stage,
            attempt=attempt,
            gate_reasons=result.reasons,
            field_errors=field_errors or [],
            artifact_version=artifact_version,
            pipeline=updated,
        )

    retried = await orchestrator.retry_pipeline_stage(
        conn,
        project_id,
        orchestrator.PipelineRetryRequest(reason=f"gate failed (attempt {attempt})"),
        actor=submitted_by,
    )
    return HandoffOutcome(
        outcome="schema_rejected" if schema_rejected else "gate_failed_retry",
        stage=stage,
        attempt=attempt,
        gate_reasons=result.reasons if not schema_rejected else [],
        field_errors=field_errors or [],
        artifact_version=artifact_version,
        pipeline=retried,
    )


async def _escalate(
    conn: AsyncConnection, project_id: uuid.UUID, *, stage: str, actor: str
) -> orchestrator.PipelineOut:
    """Exactly-once escalation (TITAN-WFL-005): pause at NEEDS_HUMAN and notify the
    project owner with a structured report — what failed, every attempt, and the
    agent's hypotheses (the gate reasons)."""
    attempts = await repo.gate_evaluations_for_stage(conn, project_id, stage)
    report_attempts = [
        {
            "attempt": row.attempt,
            "verdict": row.verdict,
            "reasons": row.reasons,
            "evaluator": f"{row.evaluator_agent}/{row.evaluator_model}@{row.prompt_version}",
        }
        for row in attempts
    ]
    updated = await orchestrator.escalate_pipeline(
        conn,
        project_id,
        actor=actor,
        reason=f"stage '{stage}' exhausted its gate retry budget",
    )
    project = await projects.get_project(conn, project_id)
    last_reasons = report_attempts[-1]["reasons"] if report_attempts else []
    await messaging.create_notification(
        conn,
        user_id=project.created_by,
        category="pipeline",
        severity="critical",
        title=f"Pipeline needs human input — stage '{stage}' failed {len(report_attempts)} times",
        body=(
            f"The '{stage}' gate exhausted its retry budget. Latest failure: "
            f"{'; '.join(str(r) for r in last_reasons) or 'see gate ledger'}. "
            f"{len(report_attempts)} attempt(s) recorded; the pipeline is paused at NEEDS_HUMAN."
        ),
        link=f"/projects/{project_id}/handoffs/gate-ledger",
        actor=actor,
    )
    return updated


def _schema_version(stage: str) -> int:
    schema = schema_for_stage(stage)
    if schema is None:
        return 1
    field = schema.model_fields.get("schema_version")
    return int(field.default) if field is not None and isinstance(field.default, int) else 1


# --- reads ------------------------------------------------------------------


async def gate_ledger(conn: AsyncConnection, project_id: uuid.UUID) -> GateLedger:
    """Export the complete pass/fail gate ledger for a project (TITAN-WFL-004)."""
    rows = await repo.gate_evaluations(conn, project_id)
    return GateLedger(
        project_id=project_id, evaluations=[GateEvaluationOut.model_validate(r) for r in rows]
    )


async def traceability(conn: AsyncConnection, project_id: uuid.UUID) -> Traceability:
    """The TITAN-WFL-006 traceability table: every brief feature → stories → tasks →
    PRs → tests → docs, generated from the project's handoff artifacts, with gap
    detection over the MUST-features."""

    async def body_of(stage: str) -> dict[str, Any] | None:
        row = await repo.latest_artifact(conn, project_id, stage)
        return dict(row.body) if row is not None else None

    return build_traceability(
        brief=await body_of("ceo_vision"),
        backlog=await body_of("pm_backlog"),
        plan=await body_of("pmgr_plan"),
        manifest=await body_of("engineering"),
        release_approval=await body_of("ceo_release"),
    )


async def latest_artifact(
    conn: AsyncConnection, project_id: uuid.UUID, stage: str
) -> HandoffArtifactOut:
    """The most recent artifact submitted for a stage (404 if none)."""
    if schema_for_stage(stage) is None:
        raise _bad_stage(stage)
    row = await repo.latest_artifact(conn, project_id, stage)
    if row is None:
        raise not_found("artifact")
    return HandoffArtifactOut.model_validate(row)


async def artifact_versions(
    conn: AsyncConnection, project_id: uuid.UUID, stage: str
) -> ArtifactVersions:
    """Every version of a stage's artifact, newest first (metadata only).

    The rows have always been here: ``version`` is monotonic per (project, stage)
    and a resubmission is a new row, never an overwrite, so every artifact's full
    revision history is already on disk. Nothing could read it — the only path was
    `latest_artifact`, hard-coded to LIMIT 1 — which made the history write-only.
    This is what turns it into Vol 4 §6.8's architecture version timeline.
    Additive: no migration, no backfill.

    An empty list is a valid answer (nothing submitted yet), NOT a 404: the caller
    asked for a stage's history and "no revisions yet" is an honest history. An
    unknown STAGE is still a 422, exactly as `latest_artifact` treats it."""
    if schema_for_stage(stage) is None:
        raise _bad_stage(stage)
    rows = await repo.artifact_versions(conn, project_id, stage)
    return ArtifactVersions(
        project_id=project_id,
        stage=stage,
        versions=[ArtifactVersion.model_validate(r) for r in rows],
    )


async def artifact_at_version(
    conn: AsyncConnection, project_id: uuid.UUID, stage: str, version: int
) -> HandoffArtifactOut:
    """One specific version of a stage's artifact (404 if that version is absent).

    A diff is a function of TWO bodies (TITAN-FRT-031), and until now only the
    newest body was fetchable — so no client could compute one."""
    if schema_for_stage(stage) is None:
        raise _bad_stage(stage)
    row = await repo.artifact_at_version(conn, project_id, stage, version)
    if row is None:
        raise not_found("artifact")
    return HandoffArtifactOut.model_validate(row)


__all__ = [
    "ALL_ARTIFACT_SCHEMAS",
    "ARTIFACT_SCHEMAS",
    "PRODUCING_STAGES",
    "ArtifactVersion",
    "ArtifactVersions",
    "DeployGateOutcome",
    "Gate",
    "GateContext",
    "GateEvaluationOut",
    "GateLedger",
    "GateResult",
    "HandoffArtifactOut",
    "HandoffOutcome",
    "Traceability",
    "artifact_at_version",
    "artifact_versions",
    "evaluate_intake_gate",
    "gate_ledger",
    "latest_artifact",
    "register_gate",
    "register_standard_gates",
    "run_deploy_gate",
    "submit_artifact",
    "traceability",
]
