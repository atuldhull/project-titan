"""The failure-recovery ladder (Vol 2 §2.2, TITAN-AGT-005/010/023).

Every agent turn runs under the same four-rung ladder, configured per agent from
its ``failure_policy`` (§2.1.8, §4.x):

  1. **transient retry** — a transient LLM/tool error is retried with the SAME
     context (max 2, exponential backoff);
  2. **self-critique** — output that fails schema validation is re-invoked with
     the failure appended (max 2), never silently accepted (TITAN-AGT-023);
  3. **escalate** — rungs 1–2 exhausted → a ``ProgressReport(status="blocked")``
     to the agent's supervisor (or, for the CEO, a structured options memo to the
     human client, since it has no supervisor agent, §4.1);
  4. **supervisor action** — the supervisor receives the blocked report and
     reassigns / decomposes / escalates to the human (a normal turn on its side).

Per-agent overrides, all YAML-driven: ``escalates_to_human`` (CEO) sends rung 3 to
the human; ``skip_retries_on`` (Database Engineer's destructive migrations) skips
rungs 1–2 and escalates immediately; ``mode=fail_closed`` (Security §4.11 /
Reviewer §4.12) escalates on failure so the gated action queues rather than
proceeds (TITAN-AGT-010); ``mode=fail_open`` (Docs §4.13 / Critic §4.16) logs the
failure but never escalates/blocks the pipeline. Every rung is logged and traced.
"""

import re
from dataclasses import dataclass

from titan.agents.service import AgentDefinition
from titan.llm.service import is_transient_error
from titan.messaging.service import AgentMessage
from titan.runtime.output import OutboundMessage

# Ladder defaults (§2.2: "max 2" on each retry rung).
DEFAULT_MAX_TRANSIENT_RETRIES = 2
DEFAULT_MAX_SELF_CRITIQUE = 2
DEFAULT_BACKOFF_BASE_S = 0.5
MAX_BACKOFF_S = 30.0

# Rung labels — the OTel span suffix and the metric label for each rung.
RUNG_TRANSIENT = "rung1"
RUNG_SELF_CRITIQUE = "rung2"
RUNG_ESCALATE = "rung3"
RUNG_SUPERVISOR = "rung4"


def is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is a transient failure eligible for the rung-1 retry."""
    return is_transient_error(exc)


@dataclass(frozen=True)
class LadderConfig:
    """One agent's resolved recovery posture (from its ``failure_policy``)."""

    mode: str = "default"  # default | fail_open | fail_closed
    escalates_to_human: bool = False
    skip_retries_on: tuple[str, ...] = ()
    supervisor: str = ""
    max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES
    max_self_critique: int = DEFAULT_MAX_SELF_CRITIQUE
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S

    @classmethod
    def from_definition(
        cls,
        definition: AgentDefinition,
        *,
        max_transient_retries: int = DEFAULT_MAX_TRANSIENT_RETRIES,
        max_self_critique: int = DEFAULT_MAX_SELF_CRITIQUE,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
    ) -> "LadderConfig":
        fp = definition.failure_policy
        return cls(
            mode=fp.mode,
            escalates_to_human=fp.escalates_to_human,
            skip_retries_on=fp.skip_retries_on,
            supervisor=definition.supervisor,
            max_transient_retries=max_transient_retries,
            max_self_critique=max_self_critique,
            backoff_base_s=backoff_base_s,
        )

    @property
    def fail_open(self) -> bool:
        """Docs/Critic (§4.13/§4.16): a failed turn is logged, never escalated."""
        return self.mode == "fail_open"

    @property
    def fail_closed(self) -> bool:
        """Security/Reviewer (§4.11/§4.12): a failed turn escalates so the gated
        action queues rather than proceeds (TITAN-AGT-010) — the escalating
        default, made explicit for traces/KPIs."""
        return self.mode == "fail_closed"

    def skips_retries(self, operation_label: str) -> bool:
        """Whether this turn's operation matches a ``skip_retries_on`` class —
        the Database Engineer's destructive migrations bypass rungs 1–2 (§4.8).

        Both sides are tokenized on non-alphanumerics and singular-normalized, and
        the class must appear as a CONTIGUOUS whole-word phrase not immediately
        preceded by a negation ("non", "not", "nondestructive"). So "Destructive
        migration: drop users" matches "Destructive migrations", but "Non-destructive
        migration: add column" does NOT (whole-word phrase avoids the substring
        false-positive where "destructive" ⊂ "non-destructive"). Later phases
        replace label matching with structured operation tags."""
        label_tokens = _normalize_tokens(operation_label)
        for entry in self.skip_retries_on:
            phrase = [w.rstrip("s") for w in _normalize_tokens(entry) if len(w) >= 4]
            if phrase and _contains_phrase(label_tokens, phrase):
                return True
        return False

    def escalation_target(self) -> str:
        """Rung-3 recipient: the human client for a no-supervisor agent (CEO),
        else the supervisor agent_id."""
        return "human" if self.escalates_to_human else self.supervisor

    def backoff_delay(self, attempt: int) -> float:
        """Exponential backoff before the ``attempt``-th transient retry (§2.2),
        capped so a wedged provider never blocks a worker indefinitely."""
        return min(self.backoff_base_s * (2.0**attempt), MAX_BACKOFF_S)


# Words that negate a following skip-class phrase (so "non-destructive migration"
# does not match "destructive migration"). "nondestructive" (no separator) is
# handled naturally: it tokenizes to one word that is not "destructive".
_NEGATIONS = frozenset({"non", "not", "nondestructive"})


def _normalize_tokens(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, singular-normalize each word."""
    return [w.rstrip("s") for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _contains_phrase(label_tokens: list[str], phrase: list[str]) -> bool:
    """Whether ``phrase`` appears as a contiguous whole-word run in
    ``label_tokens``, not immediately preceded by a negation word."""
    span = len(phrase)
    for i in range(len(label_tokens) - span + 1):
        if label_tokens[i : i + span] == phrase and not (
            i > 0 and label_tokens[i - 1] in _NEGATIONS
        ):
            return True
    return False


def turn_operation_label(trigger: AgentMessage) -> str:
    """A human-meaningful label for the turn's operation, used to match
    ``skip_retries_on``. A TaskAssignment's title names the operation (e.g.
    "Destructive migration: drop users"); other messages use their type."""
    if trigger.type == "TaskAssignment":
        title = trigger.payload.get("title")
        if isinstance(title, str) and title:
            return title
    return trigger.type


def is_blocked_escalation(trigger: AgentMessage) -> bool:
    """Whether this trigger is a rung-3 blocked report a supervisor must handle
    (rung 4) — a ``ProgressReport(status="blocked")``."""
    return trigger.type == "ProgressReport" and trigger.payload.get("status") == "blocked"


def build_escalation(target: str, *, reason: str, options: str) -> OutboundMessage:
    """The rung-3 blocked report (a structured options memo for the human case).
    ``requires_ack`` so the supervisor watchdog (Phase 32) reaps it if ignored."""
    return OutboundMessage(
        type="ProgressReport",
        to_agent=target,
        priority="high",
        requires_ack=True,
        payload={
            "status": "blocked",
            "percent_complete": 0,
            "summary": reason,
            "blockers": [{"description": reason, "needs": options}],
        },
    )


__all__ = [
    "DEFAULT_BACKOFF_BASE_S",
    "DEFAULT_MAX_SELF_CRITIQUE",
    "DEFAULT_MAX_TRANSIENT_RETRIES",
    "RUNG_ESCALATE",
    "RUNG_SELF_CRITIQUE",
    "RUNG_SUPERVISOR",
    "RUNG_TRANSIENT",
    "LadderConfig",
    "build_escalation",
    "is_blocked_escalation",
    "is_transient",
    "turn_operation_label",
]
