"""Stream message envelope (TITAN-BCK-010).

Every message on every stream (and NATS JetStream at scale-out) carries this
shape, so the broker can be swapped without touching producers/consumers.
"""

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class Envelope(BaseModel):
    """The wire contract for a queued event (TITAN-BCK-010)."""

    event_id: str  # UUIDv7; consumers dedup on this
    topic: str
    occurred_at: dt.datetime
    actor: str  # emitting principal: 'api' | agent id | 'system'
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    # Reserved (TITAN-OPS-016): W3C traceparent so workers resume the trace
    # context on consume. Wired when workers run agent turns (Phase 24+).
    traceparent: str | None = None

    def to_wire(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_wire(cls, raw: str) -> "Envelope":
        return cls.model_validate_json(raw)
