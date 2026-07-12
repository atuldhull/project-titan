"""The pure clarification policy (Vol 6 §2.4, TITAN-WFL-007) — no I/O, so it is
deterministically fixture-testable.

Given the questions and assumptions a CEO turn proposes, the engine enforces the
policy: a question is *asked* only when its answer is (a) not inferable from the
request or organization memory, (b) not covered by a canonical default, and (c)
would materially change the brief — capped at five, in a single batch. Every other
unknown becomes a logged, user-overridable assumption. Asking a canonical-default
question (e.g. "which database?") is a policy violation the engine forecloses by
converting it to an assumption rather than a question.
"""

from collections.abc import Sequence
from dataclasses import dataclass

#: Hard policy cap (TITAN-WFL-007): at most five questions per project, one batch.
MAX_CLARIFICATION_QUESTIONS = 5

#: Topics the canonical stack (Vol 1) already decides — a question about any of
#: these is answerable from the canonical default and MUST NOT be asked (e.g.
#: "which database?"). The engine converts such a question to a logged assumption.
CANONICAL_DEFAULT_TOPICS = frozenset(
    {
        "database",
        "cache",
        "language",
        "runtime",
        "backend framework",
        "frontend framework",
        "orm",
        "message bus",
        "vector store",
        "deployment",
        "authentication",
        "observability",
    }
)

# Assumption provenance — how an unknown was resolved without asking.
SOURCE_PROPOSED = "proposed"  # the CEO logged it directly
SOURCE_CANONICAL_DEFAULT = "canonical_default"  # answered by the canonical stack
SOURCE_ORG_MEMORY = "org_memory"  # inferable from the request / org memory
SOURCE_EXCESS = "excess_question"  # dropped because the 5-question budget was full
SOURCE_TIMEOUT = "timeout_best_judgment"  # unanswered within the window (TITAN-WFL-008)


def _norm_topic(topic: str) -> str:
    return " ".join(topic.lower().split())


def _covered(topic: str, topic_set: frozenset[str]) -> bool:
    """Whether ``topic`` is covered by ``topic_set`` — an exact normalized match, or
    a single-word set entry appearing as a whole word in the topic (so "database"
    covers the question topic "which database")."""
    norm = _norm_topic(topic)
    words = set(norm.split())
    return any(entry == norm or entry in words for entry in topic_set)


@dataclass(frozen=True)
class ProposedQuestion:
    """A candidate question a CEO turn proposes; ``topic`` is the machine-checkable
    subject the policy matches against canonical defaults / inferable knowledge."""

    topic: str
    question: str


@dataclass(frozen=True)
class ProposedAssumption:
    statement: str
    user_overridable: bool = True


@dataclass(frozen=True)
class Assumption:
    """A resolved, logged assumption in the brief's assumptions section. ``source``
    records how it was resolved; ``high_visibility`` marks the ones the release
    approval (H12) must restate (dropped questions, timeout best-judgments)."""

    statement: str
    source: str
    user_overridable: bool = True
    high_visibility: bool = False


@dataclass(frozen=True)
class ClarificationPlan:
    """The essential questions to ask (≤5) and every logged assumption."""

    questions: tuple[ProposedQuestion, ...] = ()
    assumptions: tuple[Assumption, ...] = ()


def resolve_policy(
    *,
    proposed_questions: Sequence[ProposedQuestion],
    proposed_assumptions: Sequence[ProposedAssumption] = (),
    canonical_default_topics: frozenset[str] = CANONICAL_DEFAULT_TOPICS,
    inferable_topics: frozenset[str] = frozenset(),
    max_questions: int = MAX_CLARIFICATION_QUESTIONS,
) -> ClarificationPlan:
    """Apply the §2.4 policy. Questions whose topic is a canonical default or is
    inferable become assumptions (never asked); duplicates collapse by topic;
    anything past the ``max_questions`` budget becomes a high-visibility assumption.
    The remaining questions are the essential batch."""
    canonical = frozenset(_norm_topic(t) for t in canonical_default_topics)
    inferable = frozenset(_norm_topic(t) for t in inferable_topics)

    assumptions: list[Assumption] = [
        Assumption(a.statement, SOURCE_PROPOSED, a.user_overridable) for a in proposed_assumptions
    ]
    essential: list[ProposedQuestion] = []
    seen_topics: set[str] = set()

    for q in proposed_questions:
        topic = _norm_topic(q.topic)
        if _covered(q.topic, canonical):
            assumptions.append(
                Assumption(
                    f"'{q.topic}' uses the canonical default (not asked — TITAN-WFL-007).",
                    SOURCE_CANONICAL_DEFAULT,
                )
            )
            continue
        if _covered(q.topic, inferable):
            assumptions.append(
                Assumption(
                    f"'{q.topic}' inferred from the request or organization memory (not asked).",
                    SOURCE_ORG_MEMORY,
                )
            )
            continue
        if topic in seen_topics:
            continue
        seen_topics.add(topic)
        essential.append(q)

    kept = essential[:max_questions]
    for dropped in essential[max_questions:]:
        assumptions.append(
            Assumption(
                f"'{dropped.topic}' deferred to a best-judgment assumption "
                f"(the {max_questions}-question budget was reached).",
                SOURCE_EXCESS,
                high_visibility=True,
            )
        )
    return ClarificationPlan(questions=tuple(kept), assumptions=tuple(assumptions))


def timeout_assumptions(unanswered_questions: Sequence[str]) -> tuple[Assumption, ...]:
    """The high-visibility assumptions recorded when a batch times out unanswered
    (TITAN-WFL-008): each unanswered question proceeds on a best-judgment default
    that the release approval must restate."""
    return tuple(
        Assumption(
            f"Proceeded without an answer to: {q} — best-judgment default applied (timeout).",
            SOURCE_TIMEOUT,
            high_visibility=True,
        )
        for q in unanswered_questions
    )


__all__ = [
    "CANONICAL_DEFAULT_TOPICS",
    "MAX_CLARIFICATION_QUESTIONS",
    "SOURCE_CANONICAL_DEFAULT",
    "SOURCE_EXCESS",
    "SOURCE_ORG_MEMORY",
    "SOURCE_PROPOSED",
    "SOURCE_TIMEOUT",
    "Assumption",
    "ClarificationPlan",
    "ProposedAssumption",
    "ProposedQuestion",
    "resolve_policy",
    "timeout_assumptions",
]
