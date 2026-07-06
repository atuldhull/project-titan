"""Redis channel + key naming for the WS fan-out (Vol 3 §5, Vol 4 §5.2).

One place owns every channel/key string so the fan-out publisher, the per-replica
subscriber, and the buffer agree. Project events fan out on ``ws.project.{id}``
(TITAN-BCK-007) and are replayable from ``ws.buffer.{id}``; the Vol 4 §5.2 scoped
channels — ``notifications:{userId}`` (user-scoped), ``agents:status`` (global),
``org:{oid}:activity`` — carry the always-on shell topics.
"""

import uuid

# --- pub/sub channels ------------------------------------------------------

AGENTS_STATUS_CHANNEL = "agents:status"


def project_channel(project_id: uuid.UUID | str) -> str:
    return f"ws.project.{project_id}"


def user_channel(user_id: uuid.UUID | str) -> str:
    return f"notifications:{user_id}"


def org_channel(org: str) -> str:
    return f"org:{org}:activity"


# Patterns each api replica psubscribes to (one subscriber forwards to all its
# local sockets). ``agents:status`` is a literal channel; the rest are patterns.
SUBSCRIBE_PATTERNS: tuple[str, ...] = (
    "ws.project.*",
    "notifications:*",
    "org:*:activity",
)
SUBSCRIBE_CHANNELS: tuple[str, ...] = (AGENTS_STATUS_CHANNEL,)


# --- per-project replay buffer + sequence ----------------------------------


def buffer_key(project_id: uuid.UUID | str) -> str:
    return f"ws.buffer.{project_id}"


def project_seq_key(project_id: uuid.UUID | str) -> str:
    return f"ws.seq.{project_id}"


def agents_seq_key() -> str:
    return "ws.seq.agents"


def user_seq_key(user_id: uuid.UUID | str) -> str:
    return f"ws.seq.user.{user_id}"


def org_seq_key(org: str) -> str:
    return f"ws.seq.org.{org}"


# --- per-user connection-cap registry --------------------------------------


def conns_key(user_id: uuid.UUID | str) -> str:
    return f"ws.conns.{user_id}"


__all__ = [
    "AGENTS_STATUS_CHANNEL",
    "SUBSCRIBE_CHANNELS",
    "SUBSCRIBE_PATTERNS",
    "agents_seq_key",
    "buffer_key",
    "conns_key",
    "org_channel",
    "org_seq_key",
    "project_channel",
    "project_seq_key",
    "user_channel",
    "user_seq_key",
]
