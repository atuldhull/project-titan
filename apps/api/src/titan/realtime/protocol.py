"""WebSocket wire protocol: the fixed server envelope + client-frame parsing
(Vol 3 §5 TITAN-BCK-007, ADR-009).

Every server → client frame is JSON with the fixed envelope
``{v, type, seq, ts, project_id, payload}`` (§5). ``type`` is one of the ten
server event types plus the control frames (``subscribed``/``pong``/``error``/
``sys.resync``). ``seq`` is a monotonically increasing integer scoped to the
frame's channel (per-project for project frames — see :mod:`titan.realtime.buffer`);
a connection observing a channel therefore sees a strictly increasing ``seq``,
which is the "per-connection monotonic" contract the spec states.

Client → server frames (§5 + item 3 in-band re-auth): ``subscribe``,
``unsubscribe``, ``ping``, ``authenticate``. Parsing is strict — a malformed
frame yields a :class:`ProtocolError` the endpoint reports as a typed ``error``
frame rather than crashing the socket.
"""

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

PROTOCOL_VERSION = 1

# --- server event types (Vol 3 §5 table — all ten) -------------------------
TASK_CREATED = "task.created"
TASK_UPDATED = "task.updated"
RUN_STARTED = "run.started"
RUN_FINISHED = "run.finished"
RUN_OUTPUT_DELTA = "run.output.delta"
MESSAGE_CREATED = "message.created"
DECISION_RECORDED = "decision.recorded"
AGENT_STATUS = "agent.status"
SPRINT_UPDATED = "sprint.updated"
SYS_NOTICE = "sys.notice"
# Vol 6 §2.5 / §2.1 (Phase 41): the Kanban card-move and pipeline stage frames.
TASK_KANBAN = "task.kanban"
PIPELINE_UPDATED = "pipeline.updated"

SERVER_EVENT_TYPES: frozenset[str] = frozenset(
    {
        TASK_CREATED,
        TASK_UPDATED,
        RUN_STARTED,
        RUN_FINISHED,
        RUN_OUTPUT_DELTA,
        MESSAGE_CREATED,
        DECISION_RECORDED,
        AGENT_STATUS,
        SPRINT_UPDATED,
        SYS_NOTICE,
        TASK_KANBAN,
        PIPELINE_UPDATED,
    }
)

# --- control frames (server-originated, not part of the §5 domain taxonomy) -
SUBSCRIBED = "subscribed"  # ack of a subscribe (carries the assigned topics)
UNSUBSCRIBED = "unsubscribed"
PONG = "pong"
ERROR = "error"
SYS_RESYNC = "sys.resync"  # reconnect gap beyond the buffer → client refetches
PING = "ping"
AUTHENTICATED = "authenticated"  # ack of a successful in-band re-auth

# The Vol 4 §5.2 "always-on shell" topics every connection is auto-subscribed
# to: the bell (own-user notifications) and the global agent-status badges.
TOPIC_NOTIFICATIONS_ME = "notifications:me"
TOPIC_AGENTS_STATUS = "agents:status"
TOPIC_ORG_ACTIVITY = "org:activity"


def now_iso() -> str:
    """RFC-3339 UTC timestamp with a trailing ``Z`` (matches the §5 example)."""
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_frame(
    frame_type: str,
    *,
    seq: int,
    payload: dict[str, Any],
    project_id: str | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Assemble a server frame in the fixed §5 envelope order."""
    return {
        "v": PROTOCOL_VERSION,
        "type": frame_type,
        "seq": seq,
        "ts": ts or now_iso(),
        "project_id": project_id,
        "payload": payload,
    }


def encode(frame: dict[str, Any]) -> str:
    return json.dumps(frame, separators=(",", ":"))


def error_frame(code: str, message: str, *, project_id: str | None = None) -> dict[str, Any]:
    """A typed ``error`` control frame (seq 0 — control frames are not sequenced)."""
    return build_frame(
        ERROR, seq=0, payload={"code": code, "message": message}, project_id=project_id
    )


# --- client → server frames ------------------------------------------------


class ProtocolError(Exception):
    """A client frame was malformed or of an unknown type."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SubscribeFrame:
    project_id: uuid.UUID | None
    topics: list[str] = field(default_factory=list)
    last_seq: int | None = None


@dataclass(frozen=True)
class UnsubscribeFrame:
    project_id: uuid.UUID | None
    topics: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PingFrame:
    pass


@dataclass(frozen=True)
class PongFrame:
    pass


@dataclass(frozen=True)
class AuthenticateFrame:
    token: str


ClientFrame = SubscribeFrame | UnsubscribeFrame | PingFrame | PongFrame | AuthenticateFrame


def _as_uuid(raw: Any, kind: Literal["subscribe", "unsubscribe"]) -> uuid.UUID | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise ProtocolError("bad_frame", f"{kind}.project_id is not a valid UUID") from exc


def _as_topics(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise ProtocolError("bad_frame", "topics must be a list of strings")
    return raw


def parse_client_frame(raw: str) -> ClientFrame:
    """Parse one text frame from the client. Raises :class:`ProtocolError` on
    malformed JSON, a missing/unknown discriminator, or bad field types."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProtocolError("bad_frame", "frame is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ProtocolError("bad_frame", "frame must be a JSON object")

    # Accept ``type`` (Vol 3 §5) or ``op`` (Vol 4 §5.1) as the discriminator.
    kind = data.get("type") or data.get("op")
    if kind in ("subscribe", "sub"):
        last_seq = data.get("last_seq")
        if last_seq is not None and not isinstance(last_seq, int):
            raise ProtocolError("bad_frame", "last_seq must be an integer")
        return SubscribeFrame(
            project_id=_as_uuid(data.get("project_id"), "subscribe"),
            topics=_as_topics(data.get("topics")),
            last_seq=last_seq,
        )
    if kind in ("unsubscribe", "unsub"):
        return UnsubscribeFrame(
            project_id=_as_uuid(data.get("project_id"), "unsubscribe"),
            topics=_as_topics(data.get("topics")),
        )
    if kind == "ping":
        return PingFrame()
    if kind == "pong":
        return PongFrame()
    if kind == "authenticate":
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise ProtocolError("bad_frame", "authenticate.token must be a non-empty string")
        return AuthenticateFrame(token=token)
    raise ProtocolError("unknown_frame", f"unknown client frame type: {kind!r}")


__all__ = [
    "AGENT_STATUS",
    "AUTHENTICATED",
    "DECISION_RECORDED",
    "ERROR",
    "MESSAGE_CREATED",
    "PING",
    "PIPELINE_UPDATED",
    "PONG",
    "PROTOCOL_VERSION",
    "RUN_FINISHED",
    "RUN_OUTPUT_DELTA",
    "RUN_STARTED",
    "SERVER_EVENT_TYPES",
    "SPRINT_UPDATED",
    "SUBSCRIBED",
    "SYS_NOTICE",
    "SYS_RESYNC",
    "TASK_CREATED",
    "TASK_KANBAN",
    "TASK_UPDATED",
    "TOPIC_AGENTS_STATUS",
    "TOPIC_NOTIFICATIONS_ME",
    "TOPIC_ORG_ACTIVITY",
    "UNSUBSCRIBED",
    "AuthenticateFrame",
    "ClientFrame",
    "PingFrame",
    "PongFrame",
    "ProtocolError",
    "SubscribeFrame",
    "UnsubscribeFrame",
    "build_frame",
    "encode",
    "error_frame",
    "now_iso",
    "parse_client_frame",
]
