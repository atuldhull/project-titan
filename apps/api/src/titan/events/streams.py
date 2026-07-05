"""Canonical stream names (Vol 3 §7, ADR-BCK-02)."""

STREAM_TASKS_READY = "stream:tasks.ready"
STREAM_RUNS_EXECUTE = "stream:runs.execute"
STREAM_RUNS_RESULTS = "stream:runs.results"
STREAM_EVENTS_DOMAIN = "stream:events.domain"

ALL_STREAMS = [
    STREAM_TASKS_READY,
    STREAM_RUNS_EXECUTE,
    STREAM_RUNS_RESULTS,
    STREAM_EVENTS_DOMAIN,
]


def dlq_stream(stream: str) -> str:
    """Poison-message dead-letter stream for ``stream`` (ADR-BCK-02)."""
    return f"{stream}.dlq"
