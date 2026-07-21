"""Live tool-output streaming over Redis pub/sub (TITAN-INT-031).

``terminal.run`` publishes each captured output chunk as a ``tool.output`` event
on the project's pub/sub channel so the dashboard Logs view can tail a running
command incrementally. Pub/sub (not the durable Streams bus) is deliberate: live
logs are ephemeral and fire-and-forget — a subscriber that isn't listening
simply misses them, and the full output is still captured in the tool result.

Chunks pass the bus's redactor before publishing (TITAN-SEC-046/054 — sandbox
output reaches Redis, and from there potentially logs/UI, so it is a sink).
Docker frames split arbitrarily, so redaction is LINE-buffered: complete lines
publish immediately, a partial tail is held until its newline arrives (or the
hold cap forces it out) so a token split across two frames cannot straddle the
redactor. The bus calls :meth:`flush` when the call finishes to release the
last unterminated line.

When no Redis is wired the stream is a silent no-op (unit tests, zero-config).
"""

import contextlib
import json
import uuid
from collections.abc import Callable

from redis.asyncio import Redis

# A pathological no-newline stream must not buffer unboundedly; past this the
# partial line is redacted and published as-is (a secret straddling THIS forced
# boundary is still caught by the envelope-level redaction of the full result).
_MAX_HOLD_CHARS = 32_768

_REDACTION_FAILURE_MARKER = "[REDACTION-ERROR]\n"


def channel_for(project_id: uuid.UUID) -> str:
    return f"tool.output:{project_id}"


class ToolOutputStream:
    """A per-call publisher pre-bound to one tool invocation's identity."""

    def __init__(
        self,
        redis: Redis | None,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
        agent_id: str,
        tool: str,
        tool_call_id: str,
        redactor: Callable[[str], str] | None = None,
    ) -> None:
        self._redis = redis
        self._channel = channel_for(project_id)
        self._task_id = str(task_id) if task_id is not None else None
        self._agent_id = agent_id
        self._tool = tool
        self._tool_call_id = tool_call_id
        self._seq = 0
        self._redactor = redactor
        self._pending: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    async def emit(self, stream: str, chunk: str) -> None:
        """Buffer, redact by complete line, and publish one ``tool.output``
        chunk. Best-effort: a Redis hiccup on a live-log publish must never
        fail the tool call."""
        if self._redis is None or not chunk:
            return
        if self._redactor is None:
            await self._publish(stream, chunk)
            return
        buffered = self._pending.get(stream, "") + chunk
        complete, sep, tail = buffered.rpartition("\n")
        if sep:
            self._pending[stream] = tail
            await self._publish(stream, self._safe_redact(complete + sep))
        elif len(buffered) > _MAX_HOLD_CHARS:
            self._pending[stream] = ""
            await self._publish(stream, self._safe_redact(buffered))
        else:
            self._pending[stream] = buffered

    async def flush(self) -> None:
        """Publish any held partial lines (call ends, stream closes)."""
        for stream in list(self._pending):
            tail = self._pending.pop(stream)
            if tail:
                await self._publish(stream, self._safe_redact(tail))

    def _safe_redact(self, text: str) -> str:
        """Fail CLOSED: a redactor crash drops the chunk's content, never
        publishes it raw."""
        try:
            return self._redactor(text) if self._redactor is not None else text
        except Exception:
            return _REDACTION_FAILURE_MARKER

    async def _publish(self, stream: str, chunk: str) -> None:
        if self._redis is None or not chunk:
            return
        seq = self._seq
        self._seq += 1
        payload = json.dumps(
            {
                "type": "tool.output",
                "tool_call_id": self._tool_call_id,
                "task_id": self._task_id,
                "agent_id": self._agent_id,
                "tool": self._tool,
                "stream": stream,
                "seq": seq,
                "chunk": chunk,
            }
        )
        with contextlib.suppress(Exception):  # never fail a call on a log publish
            await self._redis.publish(self._channel, payload)


__all__ = ["ToolOutputStream", "channel_for"]
