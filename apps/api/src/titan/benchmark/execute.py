"""Execute the ``todo-tiny`` brief end-to-end (Phase 55 DoD item 3, scope item 4).

Drives the H1→H12 delivery pipeline to a released state at the service layer (no
HTTP) with deterministic mock-LLM artifacts, then gathers the raw signals the KPI
report needs: completion, first-draft rejection (the induced G2 failure), time-to-
first-deploy, gateway-tracked spend, and the human-intervention count.

Gate configuration mutates the process-global handoffs gate registry (as the app
does at startup); the caller owns isolation (the CLI runs in a fresh process, the
tests wrap this in a gate-registry fixture).
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.benchmark import tiny_brief
from titan.benchmark.suite import Brief
from titan.clarification import service as clarification
from titan.db import uuid7
from titan.handoffs import gates
from titan.handoffs import service as handoffs
from titan.handoffs.service import register_standard_gates
from titan.llm import repository as llm_repo
from titan.llm.models import ChatRequest, Message, RequestMetadata
from titan.orchestrator import service as orchestrator
from titan.projects import service as projects
from titan.projects.service import ProjectCreate


@dataclass
class BriefRunResult:
    """The raw signals from one executed brief, before KPI aggregation."""

    brief_id: str
    project_id: uuid.UUID
    completed: bool
    first_draft_rejected: bool
    time_to_first_deploy_seconds: float | None
    cost_usd: float
    cost_measured: bool
    human_intervention_count: int


def configure_pipeline_gates() -> None:
    """Register the real G1–G12 gates, overriding only the engineering gate (G6):
    it reads execution ``checks`` a mock run does not wire, so it fails closed —
    the override lets the deterministic pipeline advance while G2–G5 and G7–G12
    are evaluated for real (matches the Phase-51 E2E harness)."""
    gates.clear_gates()
    register_standard_gates()
    gates.register_gate("engineering", gates.ConfigurableGate(passes=True))


async def execute_tiny_brief(
    engine: AsyncEngine,
    brief: Brief,
    *,
    created_by: uuid.UUID,
    gateway: Any | None = None,
    clock: Callable[[], float] | None = None,
    configure_gates: Callable[[], None] | None = None,
) -> BriefRunResult:
    """Run ``brief`` (the executable one) to release. ``gateway`` — when provided —
    meters a few attributed LLM calls so the cost KPI reads a real gateway ledger
    figure; omit it for a zero-spend run."""
    clk = clock or time.monotonic
    (configure_gates or configure_pipeline_gates)()
    t0 = clk()

    # A per-run nonce keeps the project name AND brief text unique: gate G1 (H1)
    # rejects a UserRequest whose brief duplicates an already-active project, so
    # two benchmark runs of the same brief would otherwise collide.
    nonce = uuid7().hex
    run_prompt = f"{brief.prompt} [benchmark run {nonce}]"
    async with engine.begin() as conn:
        project = await projects.create_project(
            conn,
            ProjectCreate(
                name=f"bench-{brief.id}-{nonce}",
                display_name=brief.title,
                brief=run_prompt,
            ),
            created_by,
        )
    pid = project.id

    # H1: clarification. Six proposed questions exceed the 5-question budget, so one
    # drops to a high-visibility assumption; the asked questions are human touches.
    batch = await clarification.submit_user_request(
        engine,
        pid,
        clarification.UserRequestSubmission(
            raw_text=run_prompt,
            proposed_questions=[
                clarification.ProposedQuestionIn(topic=topic, question=question)
                for topic, question in tiny_brief.CLARIFICATION_QUESTIONS
            ],
        ),
        submitted_by="agent.ceo",
    )
    questions_asked = len(batch.questions)

    # H2: induced G2 failure → retry → pass (proves the loop bites).
    bad = await handoffs.submit_artifact(
        engine, pid, "ceo_vision", tiny_brief.BAD_BRIEF, submitted_by="agent.ceo"
    )
    first_draft_rejected = bad.outcome == "gate_failed_retry"
    good = await handoffs.submit_artifact(
        engine, pid, "ceo_vision", tiny_brief.BRIEF, submitted_by="agent.ceo"
    )
    if good.outcome != "advanced":
        raise RuntimeError(f"ceo_vision did not advance after retry: {good.gate_reasons}")

    # H3–H12: every remaining stage advances; stamp TTFD when deploy advances.
    ttfd: float | None = None
    for stage, body in tiny_brief.STAGES_AFTER_BRIEF:
        outcome = await handoffs.submit_artifact(
            engine, pid, stage, body, submitted_by=f"agent.{stage}"
        )
        if outcome.outcome != "advanced":
            raise RuntimeError(f"stage {stage} did not advance: {outcome.gate_reasons}")
        if stage == tiny_brief.DEPLOY_STAGE and ttfd is None:
            ttfd = clk() - t0

    async with engine.connect() as conn:
        pipeline = await orchestrator.get_pipeline(conn, pid)
    completed = pipeline.pipeline.status == "released"

    metered_calls = await _meter_agent_spend(gateway, pid) if gateway is not None else 0
    # Cost is "measured" only if a metered gateway call actually landed in the
    # ledger — a swallowed offline failure leaves $0 UNMEASURED, not a real $0.
    cost_measured = metered_calls > 0

    async with engine.connect() as conn:
        cost = float(await llm_repo.sum_cost(conn, scope="project", scope_id=pid))
        approvals = await _approval_count(conn, pid)

    return BriefRunResult(
        brief_id=brief.id,
        project_id=pid,
        completed=completed,
        first_draft_rejected=first_draft_rejected,
        time_to_first_deploy_seconds=ttfd,
        cost_usd=cost,
        cost_measured=cost_measured,
        human_intervention_count=questions_asked + approvals,
    )


async def _meter_agent_spend(gateway: Any, project_id: uuid.UUID) -> int:
    """Model the agents' LLM spend during the brief: a few attributed gateway calls
    so the cost is recorded in the real ``llm_usage`` ledger (TITAN-FND-021).
    Returns how many calls actually landed — 0 means metering was unavailable
    (offline/misconfigured), so the caller reports cost as UNMEASURED rather than a
    false $0. NOTE: against the real gateway this incurs a small real spend."""
    landed = 0
    for i in range(3):
        try:
            await gateway.chat(
                ChatRequest(
                    messages=[Message(role="user", content=f"benchmark agent step {i}")],
                    max_tokens=64,
                    metadata=RequestMetadata(
                        project_id=str(project_id), agent_id="agent.benchmark"
                    ),
                )
            )
            landed += 1
        except Exception:  # noqa: BLE001 — cost degrades to UNMEASURED offline; not a run failure
            return landed
    return landed


async def _approval_count(conn: Any, project_id: uuid.UUID) -> int:
    result = await conn.execute(
        text("SELECT count(*) FROM tool_approvals WHERE project_id = :pid"),
        {"pid": project_id},
    )
    return int(result.scalar_one())


__all__ = ["BriefRunResult", "configure_pipeline_gates", "execute_tiny_brief"]
