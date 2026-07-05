"""The pluggable run executor (Vol 3 §8.2).

A run is one agent attempt at a task. The executor is the seam where real LLM
turns + tool calls land with the agent runtime (Vol 2/5); at this phase we ship
a deterministic :class:`StubExecutor` so the whole lifecycle — pop, run, write
artifacts, account tokens/cost, publish result — is exercised end to end. The
coordinator resolves the context and persists the outcome; the executor is pure
(context in, outcome out) and never touches the DB or the queue.
"""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from titan.runs.service import ArtifactKind


@dataclass(frozen=True)
class RunContext:
    """Everything the executor needs, resolved by the coordinator (§8.2:
    task spec + input refs; memory/artifact retrieval arrives with Vol 5)."""

    run_id: uuid.UUID
    task_id: uuid.UUID
    agent_id: uuid.UUID
    project_id: uuid.UUID
    attempt: int
    spec: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ExecutedArtifact:
    kind: ArtifactKind
    data: bytes
    path: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    """The executor's result. ``timed_out`` is not produced here — the
    coordinator's soft-timeout wrapper owns that status."""

    status: Literal["succeeded", "failed"]
    output_summary: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    error: str | None = None
    artifacts: list[ExecutedArtifact] = field(default_factory=list)


@runtime_checkable
class RunExecutor(Protocol):
    async def execute(self, ctx: RunContext) -> RunOutcome:
        """Execute one attempt at a task and return its outcome + artifacts."""
        ...


class StubExecutor:
    """Deterministic placeholder until the agent runtime lands. Resolves the
    context, emits one ``doc`` artifact recording what it 'did', and reports
    fixed token/cost figures so accounting and result-driven transitions work."""

    async def execute(self, ctx: RunContext) -> RunOutcome:
        body = (
            f"# Run {ctx.run_id} (attempt {ctx.attempt})\n\n"
            f"Task: {ctx.task_id}\nAgent: {ctx.agent_id}\n\n"
            f"## Resolved spec\n{ctx.spec or '(no spec)'}\n"
        ).encode()
        return RunOutcome(
            status="succeeded",
            output_summary=f"stub executor completed task {ctx.task_id}",
            provider="stub",
            model="stub-0",
            tokens_in=128,
            tokens_out=256,
            cost_usd=Decimal("0.0000"),
            artifacts=[ExecutedArtifact(kind="doc", data=body, path=f"runs/{ctx.run_id}.md")],
        )
