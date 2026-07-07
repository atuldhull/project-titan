"""The minimal CEO intake handler — the walking-skeleton vertical slice (Phase 25).

A deliberately thin stand-in for the real CEO agent (the full roster arrives with
Phase 26): when a project brief lands it runs ONE streamed LLM turn through the
Phase-15 gateway, re-broadcasts each token as a ``run.output.delta`` over the
Phase-24 WebSocket, parses the completion into ProductBacklog user stories, and
persists them as Phase-11 tasks — whose ``task.created`` outbox events reach the
same sockets. Kept small + isolated behind this one function so the real agent
can replace it. It reuses the existing gateway, WS layer, and outbox event path:
no direct provider call, no second socket, no ad-hoc event emit.

The handler is resilient: any failure is logged, a ``run.finished`` (failed) frame
is published, and 0 is returned — a wonky local model can never crash the caller.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.intake.stories import parse_stories
from titan.llm.service import ChatRequest, LlmGateway, Message, RequestMetadata
from titan.orchestrator import service as orchestrator
from titan.orchestrator.service import TaskCreate
from titan.realtime.service import RealtimePublisher

logger = structlog.get_logger("titan.intake")

CEO_AGENT_ID = "agent.ceo"
MAX_TOKENS = 1024

_SYSTEM_PROMPT = (
    "You are the CEO agent for PROJECT TITAN. Turn the product brief into a small "
    "product backlog of user stories. Respond with ONLY a JSON array — no prose, no "
    "code fences. Each element is an object: "
    '{"title": "As a <role>, I want <capability>, so that <benefit>", '
    '"acceptance_criteria": ["<testable criterion>", "..."]}. '
    "Produce between 3 and 6 stories covering the core of the brief."
)


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
    """Run the CEO intake turn for one project brief. Returns the number of stories
    persisted (0 on any failure). Never raises."""
    run_id = uuid.uuid4()
    pid = str(project_id)
    run_ref = str(run_id)
    await publisher.publish_run_event(
        pid,
        "run.started",
        {"run_id": run_ref, "agent_id": agent_id, "project_id": pid, "status": "running"},
    )
    request = ChatRequest(
        model_alias=model_alias,
        messages=[
            Message(role="system", content=_SYSTEM_PROMPT),
            Message(role="user", content=brief),
        ],
        max_tokens=MAX_TOKENS,
        stream=True,
        metadata=RequestMetadata(agent_id=agent_id, project_id=pid),
    )
    try:
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
            },
        )
        logger.info("intake.completed", project_id=pid, stories=created)
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
