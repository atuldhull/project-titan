"""Per-call context budgets (Vol 5 §8.1; TITAN-BRN-015).

The orchestrator computes a context budget for every LLM call from the active
model's window W (the gateway model registry's ``context_window``), split into
the §8.1 slot table — each slot ``min(share · W, cap)``:

    system + agent role prompt   8 %   3k    non-evictable
    identity context             6 %   2k    non-evictable (rides INSIDE the
                                             Memory Packet as its
                                             ``identity_context`` — §6.2 rule c)
    task instruction             8 %   3k    non-evictable
    Memory Packet               30 %  12k    retrieval trims to fit
    working summary             10 %   4k    compressed, not dropped
    recent verbatim turns       28 %  12k    oldest evicted first (folded into
                                             the rolling summary, §8.2)
    response reserve            10 %   8k    the completion's room

TITAN-BRN-015: assembly NEVER issues an overflowing prompt and NEVER fails —
every slot is individually bounded and the shares sum to 100 %, so the prompt
side is ≤ W − reserve by construction; non-evictable slots that still exceed
their own bound are head-preserving truncated with an explicit marker (the
model sees that material was cut — nothing vanishes silently).
"""

import re
from dataclasses import dataclass

from titan.memory.service import count_tokens, token_cost

# The same token stream count_tokens uses (memory.chunker) — kept in lockstep
# (same regex, same per-span cost incl. the long-run billing) so a fit_text
# cut lands exactly where the counter says it should.
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
_LONG_RUN_CHARS_PER_TOKEN = 4

# §8.1 slot table: (share of W, absolute cap).
SYSTEM_SHARE, SYSTEM_CAP = 0.08, 3_000
IDENTITY_SHARE, IDENTITY_CAP = 0.06, 2_000
TASK_SHARE, TASK_CAP = 0.08, 3_000
PACKET_SHARE, PACKET_CAP = 0.30, 12_000
SUMMARY_SHARE, SUMMARY_CAP = 0.10, 4_000
RECENT_SHARE, RECENT_CAP = 0.28, 12_000
RESERVE_SHARE, RESERVE_CAP = 0.10, 8_000

# The §8.1 table assumes W = 32k for a local model; smaller windows still split
# proportionally. A registry entry without a window gets this default.
DEFAULT_CONTEXT_WINDOW = 32_768

TRUNCATION_MARKER = "\n…[truncated to fit the context budget]"


def _slot(window: int, share: float, cap: int) -> int:
    return max(1, min(int(window * share), cap))


@dataclass(frozen=True)
class ContextBudget:
    """One call's token budget per §8.1 slot."""

    window: int
    system: int
    identity: int
    task_instruction: int
    memory_packet: int
    working_summary: int
    recent_turns: int
    response_reserve: int

    @classmethod
    def for_window(cls, window: int | None) -> "ContextBudget":
        w = window or DEFAULT_CONTEXT_WINDOW
        return cls(
            window=w,
            system=_slot(w, SYSTEM_SHARE, SYSTEM_CAP),
            identity=_slot(w, IDENTITY_SHARE, IDENTITY_CAP),
            task_instruction=_slot(w, TASK_SHARE, TASK_CAP),
            memory_packet=_slot(w, PACKET_SHARE, PACKET_CAP),
            working_summary=_slot(w, SUMMARY_SHARE, SUMMARY_CAP),
            recent_turns=_slot(w, RECENT_SHARE, RECENT_CAP),
            response_reserve=_slot(w, RESERVE_SHARE, RESERVE_CAP),
        )

    @property
    def prompt_ceiling(self) -> int:
        """The hard bound on everything BEFORE the completion (TITAN-BRN-015)."""
        return self.window - self.response_reserve

    # The Memory Packet slot and the identity slot are assembled together: the
    # §6.2 packet embeds its non-evictable identity_context, and the packet
    # assembler charges identity first, then fills retrieval to 85 % of budget.
    @property
    def packet_request_tokens(self) -> int:
        return self.memory_packet + self.identity


def fit_text(text: str, budget_tokens: int) -> str:
    """Head-preserving truncation to ``budget_tokens`` with an explicit marker.
    Used for slots that may not be dropped (system, task, summary): the lead —
    where role contracts, instructions, and summary fields put their substance —
    survives, and the model SEES that a cut happened. Long unbroken runs are
    billed at their real cost and CUT MID-RUN when needed — a single giant hex
    blob cannot ride through as 'one token'."""
    if not text or count_tokens(text) <= budget_tokens:
        return text
    keep = max(1, budget_tokens - count_tokens(TRUNCATION_MARKER))
    used = 0
    cut = len(text)
    for match in _TOKEN_RE.finditer(text):
        cost = token_cost(match.start(), match.end())
        if used + cost > keep:
            remaining = keep - used
            # Spend any residual budget INSIDE this span (4 chars ≈ 1 token).
            cut = min(match.start() + max(0, remaining) * _LONG_RUN_CHARS_PER_TOKEN, match.end())
            if remaining <= 0:
                cut = match.start()
            break
        used += cost
    return text[:cut].rstrip() + TRUNCATION_MARKER


def fit_turns(turns: list[str], budget_tokens: int) -> tuple[list[str], list[str]]:
    """§8.1 recent-verbatim policy: keep the NEWEST turns whole within budget;
    everything older is evicted (the caller folds evictions into the rolling
    summary — §8.2 — so they are compressed, never silently dropped). A single
    newest turn larger than the whole slot is truncated rather than evicted:
    the triggering context must always survive."""
    kept: list[str] = []
    evicted: list[str] = []
    used = 0
    boundary_hit = False
    for turn in reversed(turns):
        cost = count_tokens(turn)
        if not boundary_hit and used + cost <= budget_tokens:
            kept.append(turn)
            used += cost
        elif not kept:
            kept.append(fit_text(turn, budget_tokens))
            used = budget_tokens
            boundary_hit = True
        else:
            # Strict suffix semantics: once one turn does not fit, IT and every
            # OLDER turn are evicted — never an interleaved keep (the transcript
            # the model sees stays contiguous-from-now).
            boundary_hit = True
            evicted.append(turn)
    kept.reverse()
    evicted.reverse()
    return kept, evicted
