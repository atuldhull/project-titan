"""The Memory Packet hook around one agent turn (Vol 5 §13, TITAN-BRN-009).

Volume 5 mandates that every agent LLM call receive its context through a typed,
cited **Memory Packet** assembled by the retrieval pipeline — never an ad-hoc raw
dump of history (TITAN-BRN-009). The real retrieval (hybrid dense + lexical → RRF
→ rerank → assemble) lands in the memory phases; Phase 29 wires only the *seam*:

- :class:`MemoryPacket` — the §6.2 typed structure the runtime splices into the
  prompt's memory slot (sections ordered procedural → semantic → episodic so
  standards dominate anecdotes, with a non-evictable ``identity_context``).
- :class:`MemoryProvider` — the async interface the turn loop calls once per turn.
- :class:`NullMemoryProvider` — the stub that returns a valid *empty* packet with
  a fresh ``packet_id`` so each turn already links to exactly one packet (the
  TITAN-BRN-009 acceptance), long before real retrieval exists.

The packet's ``render`` produces ONLY the text that goes into the prompt; nothing
here is ever written to a log or a span (TITAN-OPS-016).
"""

import uuid
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from titan.db import uuid7


class Citation(BaseModel):
    """Provenance for one included chunk so agents can cite memory in decisions
    and reviewers can audit it (§6.2 rule a)."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    chunk_id: str | None = None
    score: float | None = None


class MemoryPacketSections(BaseModel):
    """The §6.2 sections. ``identity_context`` is non-evictable; the three list
    sections are rendered procedural → semantic → episodic on purpose."""

    model_config = ConfigDict(extra="forbid")

    identity_context: str = ""
    procedural: tuple[str, ...] = ()
    semantic: tuple[str, ...] = ()
    episodic: tuple[str, ...] = ()
    working_summary: str = ""


class MemoryPacket(BaseModel):
    """A typed, cited, budget-bounded context bundle for one agent call (§6.2)."""

    model_config = ConfigDict(extra="forbid")

    packet_id: uuid.UUID = Field(default_factory=uuid7)
    agent_id: str
    task_id: uuid.UUID | None = None
    call_seq: int = 0
    sections: MemoryPacketSections = Field(default_factory=MemoryPacketSections)
    citations: tuple[Citation, ...] = ()
    token_cost: int = 0
    budget_tokens: int = 4000

    def render(self) -> str:
        """The prompt's memory slot: identity first (non-evictable), then
        procedural → semantic → episodic → working summary. Empty when the packet
        carries nothing (the null-provider case), so the slot stays blank rather
        than emitting a misleading heading."""
        blocks: list[str] = []
        s = self.sections
        if s.identity_context:
            blocks.append(s.identity_context)
        for label, items in (
            ("Procedural (standards & checklists)", s.procedural),
            ("Semantic (facts & component map)", s.semantic),
            ("Episodic (recent relevant events)", s.episodic),
        ):
            if items:
                lines = "\n".join(f"- {item}" for item in items)
                blocks.append(f"{label}:\n{lines}")
        if s.working_summary:
            blocks.append(f"Working summary:\n{s.working_summary}")
        return "\n\n".join(blocks)


class MemoryRequest(BaseModel):
    """What the runtime asks the memory service to assemble a packet for (§13
    step 1: ``Memory Packet(task, agent, budget)``)."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    call_seq: int = 0
    query: str = ""
    budget_tokens: int = 4000


class MemoryProvider(Protocol):
    """The one method the turn loop needs. The memory phases supply a real
    implementation (Qdrant + Postgres FTS + rerank) conforming to this Protocol
    and inject it into the runtime — no turn-loop change."""

    async def assemble(self, request: MemoryRequest) -> MemoryPacket: ...


class NullMemoryProvider:
    """The Phase-29 stub: a valid, empty, freshly-identified packet every turn.

    It satisfies TITAN-BRN-009's "every agent call links to exactly one
    ``packet_id``" without pretending to retrieve anything — the memory slot
    renders blank until real retrieval lands.
    """

    async def assemble(self, request: MemoryRequest) -> MemoryPacket:
        return MemoryPacket(
            agent_id=request.agent_id,
            task_id=request.task_id,
            call_seq=request.call_seq,
            budget_tokens=request.budget_tokens,
        )


__all__ = [
    "Citation",
    "MemoryPacket",
    "MemoryPacketSections",
    "MemoryProvider",
    "MemoryRequest",
    "NullMemoryProvider",
]
