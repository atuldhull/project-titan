"""Parse an LLM completion into candidate clarification questions (Vol 6 §2.4).

The CEO turn is asked for a JSON object — ``{"questions": [{"topic", "question"}],
"inferable_topics": [...]}`` — but small local models are unreliable formatters, so
this parser mirrors the story parser's tolerance: it carves out the first balanced
JSON object, and falls back to a bare ``[…]`` question list for a model that
dropped the envelope. Malformed items are dropped and the result is bounded so a
runaway completion cannot flood the policy engine.

Two things this parser deliberately does NOT do, because
:mod:`titan.clarification.engine` is the sole authority on them (TITAN-WFL-007):
the ≤5 cap and the canonical-default/inferable demotion. Everything here is
parsing and clamping — never policy. The one judgement call is dropping a question
that arrives with no ``topic``: the topic is what the policy matches against the
canonical defaults, so an untopiced question could not be policy-checked and must
not be asked.
"""

import json
from dataclasses import dataclass
from typing import Any

from titan.intake.parsing import extract_balanced

#: Runaway guard, NOT the policy cap — the engine keeps at most
#: MAX_CLARIFICATION_QUESTIONS (5) and defers the rest to high-visibility
#: assumptions. This only stops a babbling model from proposing hundreds.
MAX_PROPOSED_QUESTIONS = 12
MAX_INFERABLE_TOPICS = 24
# The clarification service's DTO caps (ProposedQuestionIn); clamp here so a
# verbose completion can never raise a validation error at submit time.
MAX_TOPIC = 200
MAX_QUESTION = 2000


@dataclass(frozen=True)
class CandidateQuestion:
    """One question the CEO proposes; ``topic`` is the machine-checkable subject the
    §2.4 policy matches against canonical defaults and inferable knowledge."""

    topic: str
    question: str


@dataclass(frozen=True)
class ClarificationDraft:
    """What one CEO turn proposes: candidate questions, plus the topics it says it
    can infer rather than ask about (the policy demotes any question that strayed
    onto one of them to a logged assumption)."""

    questions: tuple[CandidateQuestion, ...] = ()
    inferable_topics: tuple[str, ...] = ()


def parse_clarification_draft(text: str) -> ClarificationDraft:
    """Best-effort extraction of the CEO's proposed clarifications from a completion.
    An unparseable completion yields an empty draft — which is a safe outcome: the
    engine simply opens no questions and proceeds on assumptions."""
    draft = _from_object(text)
    if draft is not None:
        return draft
    # A model that dropped the envelope and emitted the bare question list.
    return ClarificationDraft(questions=_candidates(_loads(extract_balanced(text, "[", "]"))))


def _from_object(text: str) -> ClarificationDraft | None:
    """The ``{"questions": …, "inferable_topics": …}`` envelope, or None if the first
    balanced object is not one (e.g. it is the first item of a bare list)."""
    payload = _loads(extract_balanced(text, "{", "}"))
    if not isinstance(payload, dict):
        return None
    questions = payload.get("questions", payload.get("clarifications"))
    topics = payload.get("inferable_topics", payload.get("inferable"))
    if questions is None and topics is None:
        return None
    return ClarificationDraft(questions=_candidates(questions), inferable_topics=_topics(topics))


def _candidates(value: Any) -> tuple[CandidateQuestion, ...]:
    if not isinstance(value, list):
        return ()
    out: list[CandidateQuestion] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic") or "").strip()[:MAX_TOPIC]
        question = str(item.get("question") or item.get("text") or "").strip()[:MAX_QUESTION]
        # No topic → not policy-checkable → not asked (see the module docstring).
        if topic and question:
            out.append(CandidateQuestion(topic=topic, question=question))
    return tuple(out[:MAX_PROPOSED_QUESTIONS])


def _topics(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    topics = [str(t).strip()[:MAX_TOPIC] for t in value if str(t).strip()]
    return tuple(topics[:MAX_INFERABLE_TOPICS])


def _loads(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


__all__ = [
    "MAX_INFERABLE_TOPICS",
    "MAX_PROPOSED_QUESTIONS",
    "CandidateQuestion",
    "ClarificationDraft",
    "parse_clarification_draft",
]
