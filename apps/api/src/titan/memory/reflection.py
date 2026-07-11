"""Reflection loops + lessons-learned records (Vol 5 §11.2, §12,
TITAN-BRN-023/025) — private to the `memory` module.

- **Task reflection** (§12 loop 1): after a task's final verdict the acting agent
  answers a fixed five-question rubric (goal / what worked / what surprised /
  do differently / what should the org remember) in ≤ 250 tokens on the cheapest
  model tier (TITAN-BRN-017); the answer is persisted as an episodic record and
  gates the ``done`` transition (TITAN-BRN-025).
- **Lessons** (§11.2): a structured lesson record (episodic, ``record_subtype
  'lesson'``) for every incident / systemic review rejection / failed deploy /
  retrospective finding, with a lesson KG node + DERIVED_FROM edge via the
  Phase-39 structured writers.
- **Sprint retrospective** (§12 loop 2): aggregates a sprint's task reflections
  into a sprint summary record plus 0–3 lessons.

Records flow through the SAME primitives as every other memory write — the
Phase-33 ledger (repo.append_ledger) and the Phase-35 outbox (repo.insert_outbox)
— never a parallel path.
"""

import json
import uuid
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.db import uuid7
from titan.gateway.problems import ProblemException, problem_type
from titan.llm.service import ChatRequest, Message
from titan.memory import kg
from titan.memory import repository as repo
from titan.memory.chunker import ChunkContext, get_chunker

logger = structlog.get_logger("titan.memory.reflection")


def _unknown_ref_error() -> ProblemException:
    """A well-formed request that references a project/task which does not exist
    is a clean 422, not a 500 (the FK violation surfaces as IntegrityError)."""
    return ProblemException(
        status=422,
        title="Invalid request",
        detail="The request references an unknown project or task.",
        problem_type=problem_type("validation"),
    )

REFLECTION_MAX_TOKENS = 350  # ≤ 250-token target with headroom for JSON framing
_APPLICABILITY = ("project-only", "org-candidate")


# --- shared write primitive --------------------------------------------------


async def _write_episodic(
    conn: AsyncConnection,
    *,
    record_id: uuid.UUID,
    scope: str,
    title: str,
    body_md: str,
    metadata: dict[str, Any],
    importance: float,
    source_agent: str,
    actor_agent: str,
    source_task_id: uuid.UUID | None,
    kind: str = "episodic",
) -> int:
    """Write one derived record + its chunks + outbox + one ``write`` ledger row,
    in the caller's transaction (the Phase-33/35 primitives, not a new path).
    Returns the chunk count."""
    project_id = repo_parse_project(scope)
    await repo.insert_record(
        conn,
        record_id=record_id,
        kind=kind,
        scope=scope,
        project_id=project_id,
        source_agent=source_agent,
        source_task_id=source_task_id,
        title=title[:1000],
        body_md=body_md,
        metadata=json.dumps(metadata),
        importance=importance,
    )
    chunks = get_chunker("markdown").chunk(
        body_md, ChunkContext(scope=scope, kind=kind, title=title)
    )
    for chunk in chunks:
        chunk_id = uuid7()
        await repo.insert_chunk(
            conn,
            chunk_id=chunk_id,
            record_id=record_id,
            seq=chunk.seq,
            chunk_text=chunk.text,
            token_count=chunk.token_count,
        )
        await repo.insert_outbox(conn, chunk_id=chunk_id, op="upsert", collection="titan_memory")
    await repo.append_ledger(
        conn,
        actor_agent=actor_agent,
        task_id=source_task_id,
        action="write",
        record_ids=[record_id],
        detail=json.dumps({"record_subtype": metadata.get("record_subtype"), "scope": scope}),
    )
    return len(chunks)


def repo_parse_project(scope: str) -> uuid.UUID | None:
    """'project:<uuid>' → the project id; 'org' → None. Local to avoid a
    service-module import cycle (reflection is re-exported by service)."""
    if scope == "org":
        return None
    if scope.startswith("project:"):
        return uuid.UUID(scope[len("project:") :])
    raise ValueError("scope must be 'org' or 'project:<uuid>'")


# --- lessons (§11.2) ---------------------------------------------------------


class LessonIn(BaseModel):
    """The §11.2 structured lesson."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1)  # lessons START project-scoped (§11.3 promotes)
    title: str = Field(min_length=1, max_length=1000)
    context: str = Field(min_length=1, max_length=4000)
    observation: str = Field(min_length=1, max_length=4000)
    root_cause: str = Field(min_length=1, max_length=4000)
    recommendation: str = Field(min_length=1, max_length=4000)
    evidence: list[str] = Field(default_factory=list, max_length=50)
    applicability: str = "org-candidate"
    importance: float = Field(default=0.7, ge=0.0, le=1.0)
    source_task_id: uuid.UUID | None = None

    @field_validator("applicability")
    @classmethod
    def _validate_applicability(cls, value: str) -> str:
        if value not in _APPLICABILITY:
            raise ValueError(f"applicability must be one of {_APPLICABILITY}")
        return value

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        repo_parse_project(value)
        return value


class LessonResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: uuid.UUID
    node_id: uuid.UUID
    chunk_count: int


def _lesson_body(lesson: LessonIn) -> str:
    return (
        f"# {lesson.title}\n\n"
        f"**Context:** {lesson.context}\n\n"
        f"**Observation:** {lesson.observation}\n\n"
        f"**Root cause:** {lesson.root_cause}\n\n"
        f"**Recommendation:** {lesson.recommendation}"
    )


async def file_lesson(
    engine: AsyncEngine, lesson: LessonIn, *, source_agent: str, actor_agent: str
) -> LessonResult:
    """Persist a structured lesson (§11.2, TITAN-BRN-023): an episodic
    ``record_subtype='lesson'`` record (protected from pruning, TITAN-BRN-019)
    plus a lesson KG node and a DERIVED_FROM edge to its first evidence, via the
    Phase-39 helpers."""
    record_id = uuid7()
    metadata: dict[str, Any] = {
        "record_subtype": "lesson",
        "context": lesson.context,
        "observation": lesson.observation,
        "root_cause": lesson.root_cause,
        "recommendation": lesson.recommendation,
        "evidence": lesson.evidence,
        "applicability": lesson.applicability,
    }
    derived_from = (
        kg.KgNodeRef(node_type="incident", name=lesson.evidence[0][:200])
        if lesson.evidence
        else None
    )
    try:
        async with engine.begin() as conn:
            chunk_count = await _write_episodic(
                conn,
                record_id=record_id,
                scope=lesson.scope,
                title=lesson.title,
                body_md=_lesson_body(lesson),
                metadata=metadata,
                importance=lesson.importance,
                source_agent=source_agent,
                actor_agent=actor_agent,
                source_task_id=lesson.source_task_id,
            )
            node_id = await kg.record_lesson(
                conn,
                scope=lesson.scope,
                name=lesson.title,
                derived_from=derived_from,
                properties={"applicability": lesson.applicability},
                record_id=record_id,
            )
    except IntegrityError as exc:
        raise _unknown_ref_error() from exc
    return LessonResult(record_id=record_id, node_id=node_id, chunk_count=chunk_count)


# --- task reflection (§12 loop 1, TITAN-BRN-025) -----------------------------


class ReflectionRubric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    what_worked: str = Field(min_length=1)
    what_surprised: str = Field(min_length=1)
    do_differently: str = Field(min_length=1)
    org_should_remember: str = Field(min_length=1)


class ReflectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: uuid.UUID
    rubric: ReflectionRubric


_REFLECTION_PROMPT = (
    "You are reflecting on a completed task. Answer the fixed rubric in <= 250 tokens "
    "TOTAL, concise. Reply with ONLY a JSON object with exactly these string keys: "
    "goal, what_worked, what_surprised, do_differently, org_should_remember.\n\n"
    "Task context:\n{context}"
)


async def generate_task_reflection(
    engine: AsyncEngine,
    gateway: Any,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    agent_id: str,
    context: str,
    model_alias: str | None,
) -> ReflectionResult:
    """§12 loop 1: one utility-tier LLM call produces the five-field rubric, then
    persist it as an episodic reflection record for the task (TITAN-BRN-025). The
    model alias is the cheapest configured tier (TITAN-BRN-017), passed in by the
    caller; a malformed model reply raises a clean validation error."""
    response = await gateway.chat(
        ChatRequest(
            messages=[
                Message(role="user", content=_REFLECTION_PROMPT.format(context=context[:6000]))
            ],
            model_alias=model_alias,
            max_tokens=REFLECTION_MAX_TOKENS,
        )
    )
    rubric = _parse_rubric(response.text or "")
    record_id = uuid7()
    scope = f"project:{project_id}"
    metadata: dict[str, Any] = {
        "record_subtype": "reflection",
        "agent": agent_id,
        **rubric.model_dump(),
    }
    body_md = (
        f"# Task reflection\n\n"
        f"**Goal:** {rubric.goal}\n\n**What worked:** {rubric.what_worked}\n\n"
        f"**What surprised me:** {rubric.what_surprised}\n\n"
        f"**Do differently:** {rubric.do_differently}\n\n"
        f"**Org should remember:** {rubric.org_should_remember}"
    )
    try:
        async with engine.begin() as conn:
            await _write_episodic(
                conn,
                record_id=record_id,
                scope=scope,
                title=f"Reflection: task {task_id}",
                body_md=body_md,
                metadata=metadata,
                importance=0.6,
                source_agent=f"agent.{agent_id}",
                actor_agent=f"agent.{agent_id}",
                source_task_id=task_id,
            )
    except IntegrityError as exc:
        raise _unknown_ref_error() from exc
    return ReflectionResult(record_id=record_id, rubric=rubric)


def _parse_rubric(text: str) -> ReflectionRubric:
    """The first JSON object in the model reply, validated against the rubric."""
    import re

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise ValueError("reflection reply contained no JSON object")
    return ReflectionRubric.model_validate_json(match.group(0))


async def task_has_reflection(engine: AsyncEngine, task_id: uuid.UUID) -> bool:
    """The done-block hook (TITAN-BRN-025): True iff the task has a schema-valid
    reflection. The Volume-6 task state machine calls this to refuse ``done``
    without one (wiring lands with the state-machine phases)."""
    async with engine.connect() as conn:
        return await repo.has_valid_reflection(conn, task_id)


# --- sprint retrospective (§12 loop 2) ---------------------------------------


class SprintRetroResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: uuid.UUID
    reflections_aggregated: int


async def sprint_retrospective(
    engine: AsyncEngine,
    *,
    project_id: uuid.UUID,
    task_ids: list[uuid.UUID],
    actor_agent: str,
) -> SprintRetroResult:
    """§12 loop 2: aggregate a sprint's task reflections into one semantic sprint
    summary record. Deterministic (no LLM) at MVP — it stitches the persisted
    reflections' rubrics; the LLM-distilled §8.2 sprint summary is a later
    refinement."""
    scope = f"project:{project_id}"
    record_id = uuid7()
    try:
        async with engine.begin() as conn:
            reflections = await repo.reflections_for_tasks(conn, task_ids, scope=scope)
            lines: list[str] = ["# Sprint retrospective\n"]
            for row in reflections:
                meta = row.metadata if isinstance(row.metadata, dict) else json.loads(row.metadata)
                lines.append(f"- **{row.title}** — remember: {meta.get('org_should_remember', '')}")
            body_md = (
                "\n".join(lines) if reflections else "# Sprint retrospective\n\n(no reflections)"
            )
            await _write_episodic(
                conn,
                record_id=record_id,
                scope=scope,
                title="Sprint retrospective",
                body_md=body_md,
                metadata={"record_subtype": "sprint_retro", "reflections": len(reflections)},
                importance=0.6,
                source_agent=actor_agent,
                actor_agent=actor_agent,
                source_task_id=None,
                kind="semantic",
            )
    except IntegrityError as exc:
        raise _unknown_ref_error() from exc
    return SprintRetroResult(record_id=record_id, reflections_aggregated=len(reflections))


__all__ = [
    "REFLECTION_MAX_TOKENS",
    "LessonIn",
    "LessonResult",
    "ReflectionResult",
    "ReflectionRubric",
    "SprintRetroResult",
    "file_lesson",
    "generate_task_reflection",
    "sprint_retrospective",
    "task_has_reflection",
]
