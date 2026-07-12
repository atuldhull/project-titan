"""Public service interface of the ``clarification`` module (Vol 6 §2.4,
TITAN-WFL-007/008).

The CEO clarification engine: a UserRequest is submitted (H1), gate G1 rejects an
empty or duplicate request at the API layer, and the §2.4 policy turns the CEO's
proposed questions/assumptions into a persisted batch — at most five essential
questions, everything else a logged user-overridable assumption. The batch carries
a non-blocking deadline; :func:`resolve_timed_out_batches` proceeds with
best-judgment assumptions (flagged for the release approval, H12) once it passes.

Only this interface may be imported by other modules. Pydantic v2 DTOs — never ORM
rows — cross the boundary.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.clarification import repository as repo
from titan.clarification.engine import (
    CANONICAL_DEFAULT_TOPICS,
    Assumption,
    ProposedAssumption,
    ProposedQuestion,
    resolve_policy,
    timeout_assumptions,
)
from titan.events.service import STREAM_EVENTS_DOMAIN, write_event
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import not_found
from titan.handoffs.service import evaluate_intake_gate
from titan.projects import service as projects
from titan.settings import get_settings

# The realtime event type (Vol 6 §2.4). A string literal — not an import of
# ``titan.realtime.protocol`` — so the clarification domain stays independent of the
# realtime domain (TITAN-BCK-001); the fanout handler keys off the same constant.
_CLARIFICATION_UPDATED = "clarification.updated"

# --- DTOs -------------------------------------------------------------------


class ProposedQuestionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)


class ProposedAssumptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=2000)
    user_overridable: bool = True


class UserRequestSubmission(BaseModel):
    """The H1 UserRequest plus the CEO turn's proposed clarifications. ``topic`` on
    each proposed question is the machine-checkable subject the policy matches
    against canonical defaults and ``inferable_topics`` (computed by the CEO from
    the request + organization memory)."""

    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(max_length=100_000)
    proposed_questions: list[ProposedQuestionIn] = Field(default_factory=list, max_length=100)
    proposed_assumptions: list[ProposedAssumptionIn] = Field(default_factory=list, max_length=200)
    #: Topics whose answer the CEO could infer from the request/organization memory.
    inferable_topics: list[str] = Field(default_factory=list, max_length=200)
    #: Per-project override of the non-blocking window (default from settings).
    timeout_hours: int | None = Field(default=None, ge=1, le=8760)


class AnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: uuid.UUID
    answer: str = Field(min_length=1, max_length=8000)


class AnswerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[AnswerIn] = Field(min_length=1, max_length=5)


class AssumptionOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str
    source: str
    user_overridable: bool = True
    high_visibility: bool = False


class ClarificationQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ordinal: int
    topic: str
    question: str
    answer: str | None = None
    answered_at: datetime | None = None


class ClarificationBatchOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    timeout_hours: int
    deadline_at: datetime
    resolved_at: datetime | None
    resolution: str | None
    opened_by: str
    created_at: datetime
    updated_at: datetime
    questions: list[ClarificationQuestionOut]
    assumptions: list[AssumptionOut]


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type=problem_type("conflict")
    )


def _bad_request(detail: str) -> ProblemException:
    return ProblemException(
        status=400,
        title="Invalid request",
        detail=detail,
        problem_type=problem_type("validation"),
    )


def _assumption_dict(a: Assumption) -> dict[str, Any]:
    return {
        "statement": a.statement,
        "source": a.source,
        "user_overridable": a.user_overridable,
        "high_visibility": a.high_visibility,
    }


def _batch_out(batch: Row[Any], questions: list[Row[Any]]) -> ClarificationBatchOut:
    return ClarificationBatchOut(
        id=batch.id,
        project_id=batch.project_id,
        status=batch.status,
        timeout_hours=batch.timeout_hours,
        deadline_at=batch.deadline_at,
        resolved_at=batch.resolved_at,
        resolution=batch.resolution,
        opened_by=batch.opened_by,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        questions=[ClarificationQuestionOut.model_validate(q) for q in questions],
        assumptions=[AssumptionOut.model_validate(a) for a in (batch.assumptions or [])],
    )


async def _emit(conn: AsyncConnection, batch: Row[Any], *, question_count: int, actor: str) -> None:
    """Emit the clarification event (metadata only — no question/answer text, per
    TITAN-OPS-016): project + batch ids, status, and counts."""
    await write_event(
        conn,
        STREAM_EVENTS_DOMAIN,
        {
            "type": _CLARIFICATION_UPDATED,
            "clarification": {
                "project_id": str(batch.project_id),
                "batch_id": str(batch.id),
                "status": batch.status,
                "resolution": batch.resolution,
                "question_count": question_count,
                "assumption_count": len(batch.assumptions or []),
            },
        },
        actor=actor,
    )


# --- H1 intake + clarification (TITAN-WFL-007) ------------------------------


async def submit_user_request(
    engine: AsyncEngine,
    project_id: uuid.UUID,
    submission: UserRequestSubmission,
    *,
    submitted_by: str,
) -> ClarificationBatchOut:
    """Submit the UserRequest (H1) and open the clarification batch. G1 rejects an
    empty request (400) or a duplicate of an active project (409) at the API layer;
    otherwise the §2.4 policy decides the ≤5 essential questions and logs every
    other unknown as a user-overridable assumption."""
    async with engine.begin() as conn:
        # Gate G1 (H1): non-empty + not a duplicate of an active project.
        briefs = await projects.active_project_briefs(conn, exclude=project_id)
        g1 = evaluate_intake_gate(submission.raw_text, briefs)
        if not g1.passed:
            reason = "; ".join(g1.reasons)
            if any("duplicate" in r.lower() for r in g1.reasons):
                raise _conflict(reason)
            raise _bad_request(reason)

        if await repo.get_batch(conn, project_id) is not None:
            raise _conflict("a clarification batch has already been opened for this project.")

        plan = resolve_policy(
            proposed_questions=[
                ProposedQuestion(topic=q.topic, question=q.question)
                for q in submission.proposed_questions
            ],
            proposed_assumptions=[
                ProposedAssumption(statement=a.statement, user_overridable=a.user_overridable)
                for a in submission.proposed_assumptions
            ],
            canonical_default_topics=CANONICAL_DEFAULT_TOPICS,
            inferable_topics=frozenset(submission.inferable_topics),
        )
        timeout_hours = submission.timeout_hours or get_settings().clarification_timeout_hours

        try:
            batch = await repo.insert_batch(
                conn,
                project_id=project_id,
                timeout_hours=timeout_hours,
                assumptions=[_assumption_dict(a) for a in plan.assumptions],
                opened_by=submitted_by,
            )
        except IntegrityError as exc:  # concurrent open lost the unique race
            raise _conflict(
                "a clarification batch has already been opened for this project."
            ) from exc

        for ordinal, question in enumerate(plan.questions, start=1):
            await repo.insert_question(
                conn,
                batch_id=batch.id,
                project_id=project_id,
                ordinal=ordinal,
                topic=question.topic,
                question=question.question,
            )
        # Nothing essential to ask — the CEO proceeds directly on its assumptions.
        # Resolve immediately (a question-less batch cannot be answered and must not
        # linger to the non-blocking deadline, where it would be mislabeled a
        # best-judgment timeout).
        if not plan.questions:
            batch = await repo.set_batch_resolution(
                conn,
                batch_id=batch.id,
                status="answered",
                resolution="no_questions",
                assumptions=list(batch.assumptions or []),
            )
        await _emit(conn, batch, question_count=len(plan.questions), actor=submitted_by)
        questions = list(await repo.questions_for_batch(conn, batch.id))
    return _batch_out(batch, questions)


async def answer_clarification(
    engine: AsyncEngine,
    project_id: uuid.UUID,
    submission: AnswerSubmission,
    *,
    answered_by: str,
) -> ClarificationBatchOut:
    """Record the user's answers. The batch closes (``answered``) once every
    question has an answer; a batch that is not open is a 409."""
    async with engine.begin() as conn:
        batch = await repo.get_batch_for_update(conn, project_id)
        if batch is None:
            raise not_found("clarification batch")
        if batch.status != "open":
            raise _conflict(
                f"the clarification batch is {batch.status}, not open — it cannot be answered."
            )

        for answer in submission.answers:
            updated = await repo.answer_question(
                conn, batch_id=batch.id, question_id=answer.question_id, answer=answer.answer
            )
            if updated is None:
                raise _bad_request(
                    f"question {answer.question_id} does not belong to this project's batch."
                )

        if await repo.unanswered_count(conn, batch.id) == 0:
            batch = await repo.set_batch_resolution(
                conn,
                batch_id=batch.id,
                status="answered",
                resolution="answered",
                assumptions=list(batch.assumptions or []),
            )
            await _emit(conn, batch, question_count=len(submission.answers), actor=answered_by)
        questions = list(await repo.questions_for_batch(conn, batch.id))
    return _batch_out(batch, questions)


async def get_clarification(conn: AsyncConnection, project_id: uuid.UUID) -> ClarificationBatchOut:
    """The project's clarification batch with its questions + assumptions (404 if
    none has been opened)."""
    batch = await repo.get_batch(conn, project_id)
    if batch is None:
        raise not_found("clarification batch")
    questions = list(await repo.questions_for_batch(conn, batch.id))
    return _batch_out(batch, questions)


async def resolve_timed_out_batches(engine: AsyncEngine, *, actor: str = "orchestrator") -> int:
    """Proceed with best-judgment assumptions on every open batch past its deadline
    (TITAN-WFL-008). Each unanswered question becomes a high-visibility assumption
    the release approval (H12) restates. Idempotent and per-project serialized
    (FOR UPDATE) so it never races a concurrent answer. Returns how many resolved."""
    async with engine.connect() as conn:
        overdue = [row.project_id for row in await repo.overdue_open_batches(conn)]

    resolved = 0
    for project_id in overdue:
        async with engine.begin() as conn:
            batch = await repo.get_batch_for_update(conn, project_id)
            if batch is None or batch.status != "open":
                continue  # answered or resolved between the scan and the lock
            questions = await repo.questions_for_batch(conn, batch.id)
            unanswered = [q.question for q in questions if q.answer is None]
            merged = list(batch.assumptions or []) + [
                _assumption_dict(a) for a in timeout_assumptions(unanswered)
            ]
            batch = await repo.set_batch_resolution(
                conn,
                batch_id=batch.id,
                status="timed_out",
                resolution="timeout_best_judgment",
                assumptions=merged,
            )
            await _emit(conn, batch, question_count=len(unanswered), actor=actor)
            resolved += 1
    return resolved


__all__ = [
    "AnswerIn",
    "AnswerSubmission",
    "AssumptionOut",
    "ClarificationBatchOut",
    "ClarificationQuestionOut",
    "ProposedAssumptionIn",
    "ProposedQuestionIn",
    "UserRequestSubmission",
    "answer_clarification",
    "get_clarification",
    "resolve_timed_out_batches",
    "submit_user_request",
]
