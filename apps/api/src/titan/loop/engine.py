"""The pure self-improvement-loop logic (Vol 6 §3) — no I/O, so the bounds,
convergence, refute-or-fix, and oscillation rules are deterministically testable.

The engine defines the **actor ports** the loop drives (:class:`LoopDriver`) and the
immutable data crossing them. Reviewer isolation (TITAN-WFL-011) is enforced *by
construction*: the reviewer receives a :class:`ReviewerContext` whose only fields
are the PR diff, the task spec, the ArchitecturePackage excerpt, and the
constitution — it structurally cannot carry the implementer's chain-of-thought.

The service layer owns persistence + the task/conflict integration; it calls the
driver OUTSIDE any database transaction (gather-then-persist per cycle) so a slow
LLM actor never holds a DB lock.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Finding severity, ordered. "≥ minor" (the convergence threshold, TITAN-WFL-013)
# means order ≥ 1 — everything except a purely informational note.
SEVERITY_ORDER: Mapping[str, int] = {"info": 0, "minor": 1, "major": 2, "blocking": 3}
MINOR = 1

#: The nine §3.2 stages, in order.
STAGES: tuple[str, ...] = (
    "implementation",
    "review",
    "critique",
    "refactor",
    "testing",
    "security_scan",
    "perf_analysis",
    "approval",
    "merge",
)

#: How many times an engineer may refute a finding the reviewer upholds before it
#: routes to the Architect for binding arbitration (TITAN-WFL-012).
REFUTE_UPHELD_ARBITRATION_THRESHOLD = 2


@dataclass(frozen=True)
class ReviewerContext:
    """The ONLY inputs the reviewing instance receives (TITAN-WFL-011). Its fields
    are exactly the four allowed sections — no implementer chain-of-thought — so
    isolation holds by construction. ``sections()`` is the audit manifest."""

    pr_diff: str
    task_spec: str
    architecture_excerpt: str
    constitution_version: int
    constitution_body: str

    def sections(self) -> list[str]:
        return ["pr_diff", "task_spec", "architecture_excerpt", "constitution"]


@dataclass(frozen=True)
class Finding:
    """One review finding. ``fingerprint`` is its stable identity within a loop
    (re-asserting the same finding is a no-op insert and drives oscillation/new
    detection); ``hunk_key`` identifies the code hunk it concerns."""

    fingerprint: str
    severity: str
    file: str
    line: int
    rationale: str
    suggestion: str = ""
    hunk_key: str = ""


@dataclass(frozen=True)
class Disposition:
    """The engineer's response to a finding (TITAN-WFL-012): ``fixed`` or
    ``refuted`` with a written reason. ``new_hunk_state`` is the resulting content
    of the hunk, used for oscillation detection (TITAN-WFL-014)."""

    kind: str  # "fixed" | "refuted"
    reason: str
    new_hunk_state: str | None = None


@runtime_checkable
class LoopDriver(Protocol):
    """The loop's actor ports. In production these are LLM-backed agents reached
    through the gateway; in tests a scripted double satisfies the protocol. The
    engine calls them outside any DB transaction."""

    async def review(self, ctx: ReviewerContext, cycle: int) -> Sequence[Finding]:
        """Reviewer (isolated): the findings for this cycle (may be empty)."""
        ...

    async def dispose(self, finding: Finding, cycle: int) -> Disposition:
        """Engineer: fix or refute one finding."""
        ...

    async def rejudge(self, finding: Finding, disposition: Disposition, cycle: int) -> bool:
        """Reviewer: on a refutation, whether the finding is UPHELD (True) or the
        refutation is accepted (False)."""
        ...

    async def stage_report(self, stage: str, cycle: int) -> tuple[bool, str]:
        """QA/Security/Perf: whether the named §3.2 stage passes, plus a note."""
        ...


# --- pure decision helpers --------------------------------------------------


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.get(severity, MINOR)


def is_minor_plus(finding: Finding) -> bool:
    """Whether a finding counts toward convergence (severity ≥ minor)."""
    return severity_rank(finding.severity) >= MINOR


@dataclass
class OscillationTracker:
    """Detects an A→B→A edit on a single hunk within a loop (TITAN-WFL-014). Records
    each hunk's state history; a state that re-appears after changing away is
    oscillation."""

    _history: dict[str, list[str]] = field(default_factory=dict)

    def record(self, hunk_key: str, state: str) -> bool:
        """Append ``state`` to ``hunk_key``'s history; return True iff this edit
        completes an A→B→A oscillation (the state equals an earlier one but not the
        immediately-preceding one)."""
        history = self._history.setdefault(hunk_key, [])
        oscillated = state in history and (not history or history[-1] != state)
        history.append(state)
        return oscillated


@dataclass(frozen=True)
class CycleTally:
    """The convergence bookkeeping after one cycle (TITAN-WFL-013)."""

    new_minor_plus: int
    open_minor_plus: int

    @property
    def clean(self) -> bool:
        """A cycle is clean when it raised no new ≥minor findings AND leaves none
        open — the code both stabilized and is free of unresolved issues."""
        return self.new_minor_plus == 0 and self.open_minor_plus == 0


def next_consecutive_clean(previous: int, tally: CycleTally) -> int:
    """The running count of consecutive clean cycles; two triggers convergence."""
    return previous + 1 if tally.clean else 0


CONVERGENCE_STREAK = 2


__all__ = [
    "CONVERGENCE_STREAK",
    "MINOR",
    "REFUTE_UPHELD_ARBITRATION_THRESHOLD",
    "SEVERITY_ORDER",
    "STAGES",
    "CycleTally",
    "Disposition",
    "Finding",
    "LoopDriver",
    "OscillationTracker",
    "ReviewerContext",
    "is_minor_plus",
    "next_consecutive_clean",
    "severity_rank",
]
