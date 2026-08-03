"""The minimal CEO intake handler — the walking-skeleton vertical slice (Phase 25).

A deliberately thin stand-in for the real CEO agent (the full roster arrives with
Phase 26): when a project brief lands it runs streamed LLM turns through the
Phase-15 gateway, re-broadcasts each token as a ``run.output.delta`` over the
Phase-24 WebSocket, and turns the completions into durable state — the §2.4
clarification batch, then the ProductBacklog user stories persisted as Phase-11
tasks (whose ``task.created`` outbox events reach the same sockets). Kept small +
isolated behind this one function so the real agent can replace it. It reuses the
existing gateway, WS layer, clarification engine, and outbox event path: no direct
provider call, no second socket, no ad-hoc event emit, no second clarification
mechanism.

Both turns stream under ONE ``run.started``/``run.finished`` envelope and one
run id: this is a single intake run that happens to make two gateway calls, and
the UI's stream flips to "done" exactly once, at the end.

The handler is resilient: any failure is logged, a ``run.finished`` (failed) frame
is published, and 0 is returned — a wonky local model can never crash the caller.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.clarification import service as clarification
from titan.gateway.problems import ProblemException
from titan.intake.questions import parse_clarification_draft
from titan.intake.stories import parse_stories
from titan.llm.service import ChatRequest, LlmGateway, Message, RequestMetadata
from titan.orchestrator import service as orchestrator
from titan.orchestrator.service import TaskCreate
from titan.realtime.service import RealtimePublisher

logger = structlog.get_logger("titan.intake")

CEO_AGENT_ID = "agent.ceo"
MAX_TOKENS = 1024
# The clarification envelope is small (≤5 questions); it needs far less headroom
# than a backlog, and a tight ceiling keeps a babbling model cheap.
CLARIFY_MAX_TOKENS = 512

# Phase 83 (TITAN-EVO-015): the intake system prompts are NOT hardcoded here — the
# registry is the single source of truth. These refs resolve to pinned, approved,
# drift-gated prompts in evolution/prompts/, and each intake gateway call stamps the
# resolved prompt_sha so this bootstrap CEO path carries the same provenance as a
# real agent turn. The §2.4 clarify prompt only proposes candidates; the
# clarification engine remains the authority on the ≤5 cap + assumption demotion.
_BACKLOG_PROMPT_REF = "ceo_intake"
_CLARIFY_PROMPT_REF = "ceo_intake_clarify"


def _resolve_intake_prompt(ref: str) -> tuple[str, str]:
    """(system_text, prompt_sha) for an intake prompt from the evolution registry.
    Fails closed (EvolutionError) if the prompt is unapproved/drifted — the caller
    logs and degrades, never serves an ungated prompt."""
    from titan.agents.service import build_evolution_resolver
    from titan.settings import get_settings

    return build_evolution_resolver(get_settings()).resolve_standalone(ref)


async def _open_clarification_batch(
    *,
    engine: AsyncEngine,
    gateway: LlmGateway,
    publisher: RealtimePublisher,
    project_id: uuid.UUID,
    brief: str,
    model_alias: str,
    agent_id: str,
    run_id: uuid.UUID,
) -> int:
    """Run the CEO's clarification turn (H1, Vol 6 §2.4) and open the project's
    batch through the clarification engine. Returns how many questions the policy
    kept (0 if the step was skipped).

    The question TEXT reaches the UI only as ``run.output.delta`` tokens from this
    turn; the ``clarification.updated`` event the engine emits carries counts only
    (TITAN-OPS-016) and is merely the UI's signal to refetch the batch.

    This step is ADDITIVE and never raises — the backlog is what ``run_intake``
    contracts to produce, so nothing here may cost the caller its stories:

    * ``ProblemException`` — the engine refused the submission. 409 means a batch
      already exists for this project: ``clarification_batches`` is UNIQUE on
      project_id, so there is exactly one batch per project, EVER (TITAN-WFL-007),
      and a re-run of intake must not try to open a second one. 409 also covers a
      brief that duplicates an active project and 400 an empty one (gate G1). All
      of these are decisions, not faults: log and carry on to the backlog.
    * anything else — a wonky model, an unparseable completion, a DB hiccup. The
      project simply gets no clarification batch.
    """
    pid = str(project_id)
    try:
        clarify_prompt, clarify_sha = _resolve_intake_prompt(_CLARIFY_PROMPT_REF)
        chunks: list[str] = []
        request = ChatRequest(
            model_alias=model_alias,
            messages=[
                Message(role="system", content=clarify_prompt),
                Message(role="user", content=brief),
            ],
            max_tokens=CLARIFY_MAX_TOKENS,
            stream=True,
            metadata=RequestMetadata(agent_id=agent_id, project_id=pid, prompt_sha=clarify_sha),
        )
        async for delta in gateway.stream(request, run_id=run_id):
            if delta.type == "text" and delta.text:
                chunks.append(delta.text)
                await publisher.publish_delta(pid, str(run_id), delta.text, agent_id=agent_id)
        draft = parse_clarification_draft("".join(chunks))
        # The engine — not this handler — applies the ≤5 cap and demotes
        # canonical-default / inferable topics to logged assumptions.
        batch = await clarification.submit_user_request(
            engine,
            project_id,
            clarification.UserRequestSubmission(
                raw_text=brief,
                proposed_questions=[
                    clarification.ProposedQuestionIn(topic=q.topic, question=q.question)
                    for q in draft.questions
                ],
                inferable_topics=list(draft.inferable_topics),
            ),
            submitted_by=agent_id,
        )
        logger.info("intake.clarification_opened", project_id=pid, questions=len(batch.questions))
        return len(batch.questions)
    except ProblemException as exc:
        # Refused by policy/gate — no question or brief text in the log (TITAN-OPS-016).
        logger.info("intake.clarification_skipped", project_id=pid, status=exc.status)
    except Exception:
        logger.warning("intake.clarification_failed", project_id=pid)
    return 0


async def run_intake(
    *,
    engine: AsyncEngine,
    gateway: LlmGateway,
    publisher: RealtimePublisher,
    project_id: uuid.UUID,
    brief: str,
    model_alias: str,
    agent_id: str = CEO_AGENT_ID,
) -> int:
    """Run the CEO intake for one project brief: the clarification turn (§2.4, H1)
    then the backlog turn. Returns the number of stories persisted (0 on any
    failure) — clarification is additive and never changes that count. Never raises."""
    run_id = uuid.uuid4()
    pid = str(project_id)
    run_ref = str(run_id)
    await publisher.publish_run_event(
        pid,
        "run.started",
        {"run_id": run_ref, "agent_id": agent_id, "project_id": pid, "status": "running"},
    )
    # H1 first: ask what must be asked (§2.4) before proposing the backlog. Never
    # raises — the backlog runs whether or not a batch was opened.
    questions = await _open_clarification_batch(
        engine=engine,
        gateway=gateway,
        publisher=publisher,
        project_id=project_id,
        brief=brief,
        model_alias=model_alias,
        agent_id=agent_id,
        run_id=run_id,
    )
    try:
        backlog_prompt, backlog_sha = _resolve_intake_prompt(_BACKLOG_PROMPT_REF)
        request = ChatRequest(
            model_alias=model_alias,
            messages=[
                Message(role="system", content=backlog_prompt),
                Message(role="user", content=brief),
            ],
            max_tokens=MAX_TOKENS,
            stream=True,
            metadata=RequestMetadata(agent_id=agent_id, project_id=pid, prompt_sha=backlog_sha),
        )
        chunks: list[str] = []
        async for delta in gateway.stream(request, run_id=run_id):
            if delta.type == "text" and delta.text:
                chunks.append(delta.text)
                await publisher.publish_delta(pid, run_ref, delta.text, agent_id=agent_id)
        stories = parse_stories("".join(chunks))
        # Persist each story in its OWN transaction so one bad item can never roll
        # back the rest of a good backlog. Titles are already clamped by the parser;
        # this is defence in depth. A skipped story is logged WITHOUT its content
        # (no completion text in logs, TITAN-OPS-016).
        created = 0
        for story in stories:
            try:
                async with engine.begin() as conn:
                    await orchestrator.create_task(
                        conn,
                        project_id,
                        TaskCreate(title=story.title, spec=story.spec_text()),
                        actor=agent_id,
                    )
                created += 1
            except Exception:
                logger.warning("intake.story_skipped", project_id=pid)
        await publisher.publish_run_event(
            pid,
            "run.finished",
            {
                "run_id": run_ref,
                "agent_id": agent_id,
                "project_id": pid,
                "status": "succeeded",
                "stories": created,
                "questions": questions,
            },
        )
        logger.info("intake.completed", project_id=pid, stories=created, questions=questions)
        return created
    except Exception:
        logger.exception("intake.failed", project_id=pid)
        await publisher.publish_run_event(
            pid,
            "run.finished",
            {"run_id": run_ref, "agent_id": agent_id, "project_id": pid, "status": "failed"},
        )
        return 0


__all__ = ["CEO_AGENT_ID", "run_intake"]
