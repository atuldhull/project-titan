"""Live tool-output streaming over Redis pub/sub (TITAN-INT-031).

``terminal.run`` publishes each captured output chunk as a ``tool.output`` event
on the project's pub/sub channel so the dashboard Logs view can tail a running
command incrementally. Pub/sub (not the durable Streams bus) is deliberate: live
logs are ephemeral and fire-and-forget — a subscriber that isn't listening
simply misses them, and the full output is still captured in the tool result.

When no Redis is wired the stream is a silent no-op (unit tests, zero-config).
"""

import contextlib
import json
import uuid

from redis.asyncio import Redis


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
    ) -> None:
        self._redis = redis
        self._channel = channel_for(project_id)
        self._task_id = str(task_id) if task_id is not None else None
        self._agent_id = agent_id
        self._tool = tool
        self._tool_call_id = tool_call_id
        self._seq = 0

    @property
    def enabled(self) -> bool:
        return self._redis is not None

    async def emit(self, stream: str, chunk: str) -> None:
        """Publish one ``tool.output`` chunk. Best-effort: a Redis hiccup on a
        live-log publish must never fail the tool call."""
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
