"""Direct publisher onto the Phase-24 WS channels (TITAN-BCK-007).

The fan-out consumer (:mod:`titan.realtime.fanout`) is the path for *committed*
domain events. This publisher is for events produced OUTSIDE that stream —
notably the high-frequency, ephemeral ``run.output.delta`` token stream from a
live agent turn, which must NOT go through the durable outbox (one row per token
would be absurd). It publishes a properly-enveloped §5 frame straight onto
``ws.project.{id}`` so every replica's subscriber delivers it, reusing the exact
seq counter + envelope the fan-out uses (so seq stays monotonic across both
paths). Deltas are ephemeral — assigned a seq for ordering but never buffered.
"""

from typing import Any

from redis.asyncio import Redis

from titan.realtime import channels, protocol
from titan.realtime.buffer import ProjectBuffer


class RealtimePublisher:
    """Publishes §5 frames directly onto a project's WS pub/sub channel."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._buffer = ProjectBuffer(redis)

    async def publish_project(
        self,
        project_id: str,
        frame_type: str,
        payload: dict[str, Any],
        *,
        buffered: bool,
    ) -> int:
        """Assign the next per-project seq, envelope, optionally buffer for replay,
        and publish. Returns the seq."""
        seq = await self._buffer.next_project_seq(project_id)
        wire = protocol.encode(
            protocol.build_frame(frame_type, seq=seq, payload=payload, project_id=project_id)
        )
        if buffered:
            await self._buffer.append(project_id, wire)
        await self._redis.publish(channels.project_channel(project_id), wire)
        return seq

    async def publish_delta(
        self, project_id: str, run_id: str, delta: str, *, agent_id: str | None = None
    ) -> int:
        """One streaming ``run.output.delta`` token chunk (ephemeral, not buffered)."""
        payload: dict[str, Any] = {"run_id": run_id, "delta": delta}
        if agent_id is not None:
            payload["agent_id"] = agent_id
        return await self.publish_project(
            project_id, protocol.RUN_OUTPUT_DELTA, payload, buffered=False
        )

    async def publish_run_event(
        self, project_id: str, frame_type: str, payload: dict[str, Any]
    ) -> int:
        """A run lifecycle frame (``run.started`` / ``run.finished``) — buffered so
        a reconnecting client still sees it."""
        return await self.publish_project(project_id, frame_type, payload, buffered=True)


__all__ = ["RealtimePublisher"]
