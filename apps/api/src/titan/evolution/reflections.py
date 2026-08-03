"""Post-task reflections + post-project retrospectives (Vol 10 §4, EVO-008/009).

Both are agent-authored LLM notes persisted through the Vol-5 memory write path
(``memory.service.write_events``) — NOT a second pipeline. The post-task reflection
is by the EXECUTING agent (self-reflection); the retrospective is by the Project
Manager, summarized to organization memory. Both fail-safe: a failure is logged and
never blocks task/project closure (§4.1 "never blocks").
"""

import json
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from titan.evolution.prompting import fence_untrusted
from titan.evolution.resolver import EvolutionResolver
from titan.llm.models import ChatRequest, Message, RequestMetadata
from titan.observability.metrics import EVOLUTION_OPS

logger = structlog.get_logger("titan.evolution.reflections")

_REFLECTION_MAX_TOKENS = 350
_RETRO_MAX_TOKENS = 900


def _parse_json(text_value: str | None) -> dict[str, Any]:
    if not text_value:
        return {}
    try:
        start, end = text_value.index("{"), text_value.rindex("}") + 1
        data = json.loads(text_value[start:end])
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


async def generate_post_task_reflection(
    engine: AsyncEngine,
    gateway: Any,
    resolver: EvolutionResolver,
    *,
    task_id: Any,
    project_id: Any,
    agent_roster: str,
    scorecard_id: str | None,
    context: str,
    task_cost_usd: float,
) -> dict[str, Any] | None:
    """The executing agent's bounded post-task reflection (EVO-008), written as a
    `post_task_reflection` memory record. Skipped for a zero-LLM-cost task (the 2%
    cost cap is meaningless there); one small bounded call otherwise (« 2%)."""
    if task_cost_usd <= 0:
        return None
    from titan.memory import service as memory

    try:
        # Idempotent: one reflection per task. A task.terminal redelivery re-runs this
        # step (the scorecard already exists), so guard against a duplicate write.
        async with engine.connect() as conn:
            exists = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM brain.memory_records WHERE source_task_id = :tid AND "
                        "metadata->>'record_subtype' = 'post_task_reflection' LIMIT 1"
                    ),
                    {"tid": task_id},
                )
            ).first()
        if exists is not None:
            EVOLUTION_OPS.labels("reflection", "skipped").inc()
            return None
        system_text, prompt_sha = resolver.resolve_standalone("post_task_reflection")
        req = ChatRequest(
            messages=[
                Message(role="system", content=system_text),
                Message(role="user", content=f"TASK CONTEXT:\n{fence_untrusted(context[:6000])}"),
            ],
            max_tokens=_REFLECTION_MAX_TOKENS,
            metadata=RequestMetadata(
                agent_id=f"agent.{agent_roster}",
                task_id=None,  # evolution activity metered separately (EVO-020)
                project_id=None,
                prompt_sha=prompt_sha,
            ),
        )
        resp = await gateway.chat(req)
        data = _parse_json(resp.text)
        batch = memory.MemoryEventBatch(
            source_agent=f"agent.{agent_roster}",
            task_id=task_id,
            events=[
                memory.MemoryEvent(
                    kind="episodic",
                    scope=f"project:{project_id}",
                    title=f"Post-task reflection: task {task_id}",
                    body_md=(data.get("lesson") or "").strip() or "(no lesson recorded)",
                    metadata={
                        "record_subtype": "post_task_reflection",
                        "agent": agent_roster,
                        "scorecard_id": scorecard_id,
                        "went_well": data.get("went_well", []),
                        "went_poorly": data.get("went_poorly", []),
                        "lesson": data.get("lesson", ""),
                        "proposed_change": data.get("proposed_change", {}),
                        "confidence": data.get("confidence", 0.0),
                    },
                    importance=0.6,
                    source_task_id=task_id,
                )
            ],
        )
        async with engine.begin() as conn:
            await memory.write_events(
                conn,
                batch,
                source_agent=f"agent.{agent_roster}",
                actor_agent=f"agent.{agent_roster}",
            )
    except Exception:  # noqa: BLE001 — reflection must never block task closure
        logger.warning("reflection.failed", task_id=str(task_id), exc_info=True)
        EVOLUTION_OPS.labels("reflection", "failed").inc()
        return None
    logger.info("reflection.generated", task_id=str(task_id), agent=agent_roster)
    EVOLUTION_OPS.labels("reflection", "ok").inc()
    return data


async def _gather_retro_inputs(conn: Any, project_id: Any) -> dict[str, Any]:
    cards = (
        (
            await conn.execute(
                text(
                    "SELECT sc.agent_id, sc.task_class, sc.composite_score "
                    "FROM agent_scorecards sc JOIN tasks t ON t.id = sc.task_id "
                    "WHERE t.project_id = :pid"
                ),
                {"pid": project_id},
            )
        )
        .mappings()
        .all()
    )
    decisions = (
        await conn.execute(
            text(
                "SELECT title FROM decisions WHERE project_id = :pid ORDER BY created_at LIMIT 25"
            ),
            {"pid": project_id},
        )
    ).all()
    lessons = (
        await conn.execute(
            text(
                "SELECT body_md FROM brain.memory_records WHERE scope = :scope AND status = "
                "'active' AND metadata->>'record_subtype' IN "
                "('post_task_reflection','reflection','lesson') ORDER BY created_at LIMIT 25"
            ),
            {"scope": f"project:{project_id}"},
        )
    ).all()
    n = len(cards)
    mean = round(sum(float(c["composite_score"]) for c in cards) / n, 2) if n else 0.0
    return {
        "scorecard_count": n,
        "mean_composite": mean,
        "low_scores": [
            {"agent": c["agent_id"], "class": c["task_class"], "score": float(c["composite_score"])}
            for c in cards
            if float(c["composite_score"]) < 60
        ][:10],
        "decisions": [d[0] for d in decisions],
        "lessons": [row[0] for row in lessons],
    }


async def generate_retrospective(
    engine: AsyncEngine, gateway: Any, resolver: EvolutionResolver, *, project_id: Any
) -> dict[str, Any] | None:
    """The PM's post-project retrospective (EVO-009): gather scorecards + reflections
    + decisions, synthesize, and store in organization memory (subtype='retrospective',
    org scope via a system actor). Fail-safe; idempotent per project.

    The retro consumer is scalable (multiple replicas), and a re-release can redeliver
    ``pipeline.updated(released)``. A bare SELECT-then-INSERT would let two replicas
    both pass the existence check and double-write (and double-spend the LLM). A
    session advisory lock on the project serializes them: the winner generates; the
    other blocks, then re-checks and sees the committed row, so it skips. The lock is
    BLOCKING (not try-lock) with BOTH keys salted by the project id: a try-lock would
    silently drop a *different* project that happens to collide under one hashtext,
    whereas blocking makes an (astronomically unlikely, ~2^-64) collision merely
    serialize two projects for one LLM call rather than lose a retrospective."""
    from titan.memory import service as memory

    async def _exists(conn: Any) -> bool:
        row = (
            await conn.execute(
                text(
                    "SELECT 1 FROM brain.memory_records WHERE metadata->>'record_subtype' = "
                    "'retrospective' AND metadata->>'project_id' = :pid LIMIT 1"
                ),
                {"pid": str(project_id)},
            )
        ).first()
        return row is not None

    _LOCK_SQL = "pg_advisory_lock(hashtext('evo_retro_a:'||:pid), hashtext('evo_retro_b:'||:pid))"
    _UNLOCK_SQL = (
        "pg_advisory_unlock(hashtext('evo_retro_a:'||:pid), hashtext('evo_retro_b:'||:pid))"
    )
    try:
        # Session-scoped advisory lock held on this connection across the whole
        # check→generate→write so concurrent replicas cannot both proceed.
        async with engine.connect() as lock_conn:
            await lock_conn.execute(text(f"SELECT {_LOCK_SQL}"), {"pid": str(project_id)})
            try:
                if await _exists(lock_conn):
                    EVOLUTION_OPS.labels("retrospective", "skipped").inc()
                    return None
                inputs = await _gather_retro_inputs(lock_conn, project_id)
                system_text, prompt_sha = resolver.resolve_standalone("retrospective")
                req = ChatRequest(
                    messages=[
                        Message(role="system", content=system_text),
                        Message(
                            role="user",
                            content=f"PROJECT DATA:\n{fence_untrusted(json.dumps(inputs)[:8000])}",
                        ),
                    ],
                    max_tokens=_RETRO_MAX_TOKENS,
                    metadata=RequestMetadata(
                        agent_id="agent.pm_project",
                        task_id=None,
                        project_id=None,
                        prompt_sha=prompt_sha,
                    ),
                )
                resp = await gateway.chat(req)
                data = _parse_json(resp.text)
                body = (
                    f"# Retrospective\n\n{data.get('kpi_summary', '')}\n\n"
                    f"## Top lessons\n{json.dumps(data.get('top_lessons', []))}\n\n"
                    f"## Failure inventory\n{json.dumps(data.get('failure_inventory', []))}\n\n"
                    f"## Reuse candidates\n{json.dumps(data.get('reuse_candidates', []))}"
                )
                batch = memory.MemoryEventBatch(
                    source_agent="agent.pm_project",
                    task_id=None,
                    events=[
                        memory.MemoryEvent(
                            kind="semantic",
                            scope="org",
                            title=f"Retrospective: project {project_id}",
                            body_md=body,
                            metadata={
                                "record_subtype": "retrospective",
                                "project_id": str(project_id),
                                "kpi_summary": data.get("kpi_summary", ""),
                                "top_lessons": data.get("top_lessons", []),
                                "failure_inventory": data.get("failure_inventory", []),
                                "reuse_candidates": data.get("reuse_candidates", []),
                                "proposed_eps": data.get("proposed_eps", []),
                                # CEO countersign lands here (EVO-009); empty until then.
                                "countersigned_by": "",
                                "scorecard_count": inputs["scorecard_count"],
                            },
                            importance=0.85,
                        )
                    ],
                )
                async with engine.begin() as conn:
                    await memory.write_events(
                        conn,
                        batch,
                        source_agent="system.evolution",
                        actor_agent="system.evolution",
                    )
            finally:
                await lock_conn.execute(text(f"SELECT {_UNLOCK_SQL}"), {"pid": str(project_id)})
    except Exception:  # noqa: BLE001 — retrospective must never block project closure
        logger.warning("retrospective.failed", project_id=str(project_id), exc_info=True)
        EVOLUTION_OPS.labels("retrospective", "failed").inc()
        return None
    logger.info("retrospective.generated", project_id=str(project_id))
    EVOLUTION_OPS.labels("retrospective", "ok").inc()
    return data


async def reflect_on_scorecard(
    engine: AsyncEngine, gateway: Any, resolver: EvolutionResolver, scorecard: dict[str, Any]
) -> dict[str, Any] | None:
    """Fire the executing agent's post-task reflection right after its scorecard
    (EVO-008). Pulls project/cost/context from the task; fully best-effort."""
    import uuid as _uuid

    from titan.evolution import repository as repo

    try:
        task_id = _uuid.UUID(scorecard["task_id"])
    except (ValueError, KeyError, TypeError):
        return None
    meta = await repo.scorecard_task_meta(engine, task_id)
    if meta is None:
        return None
    context = f"{meta['title']}\n{meta['spec']}\nterminal={meta['status']}"
    return await generate_post_task_reflection(
        engine,
        gateway,
        resolver,
        task_id=task_id,
        project_id=meta["project_id"],
        agent_roster=scorecard["agent_id"],
        scorecard_id=scorecard["id"],
        context=context,
        task_cost_usd=float(meta["cost_usd"]),
    )


__all__ = [
    "generate_post_task_reflection",
    "generate_retrospective",
    "reflect_on_scorecard",
]
