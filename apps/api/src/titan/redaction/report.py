"""Redaction-hit collection for the audit trail (TITAN-SEC-054 "redaction
hits emit audit events", SEC-092-safe: class + hash8 + count, never values).

``record`` runs synchronously on the redaction hot path (including inside the
log processor), so it must be non-blocking, bounded, and never raise: hits
deduplicate into a capped counter map and a composition-root flusher drains
them periodically into the vault's ``secret_audit`` ledger. When the map is
full, further NEW keys are counted in a single overflow bucket rather than
grown without bound (a secret leaked in a tight loop is one hot key, not a
memory leak).
"""

import threading
from collections.abc import Sequence
from dataclasses import dataclass

from titan.redaction.engine import Hit

OVERFLOW_SINK = "_all"
OVERFLOW_CLASS = "_overflow"


@dataclass(frozen=True, slots=True)
class RedactionHitRecord:
    sink: str
    cls: str
    hash8: str
    count: int


class RedactionReporter:
    """Bounded, deduplicating, thread-safe hit collector."""

    def __init__(self, *, max_entries: int = 1024) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, str, str], int] = {}
        self._max_entries = max_entries
        self._overflow = 0

    def record(self, sink: str, hits: Sequence[Hit]) -> None:
        with self._lock:
            for hit in hits:
                key = (sink, hit.cls, hit.hash8)
                if key in self._counts:
                    self._counts[key] += 1
                elif len(self._counts) < self._max_entries:
                    self._counts[key] = 1
                else:
                    self._overflow += 1

    def merge(self, records: Sequence[RedactionHitRecord]) -> None:
        """Re-queue drained records (a failed audit flush must not lose them)."""
        with self._lock:
            for record in records:
                key = (record.sink, record.cls, record.hash8)
                if key in self._counts:
                    self._counts[key] += record.count
                elif len(self._counts) < self._max_entries:
                    self._counts[key] = record.count
                else:
                    self._overflow += record.count

    def drain(self) -> list[RedactionHitRecord]:
        """Snapshot-and-clear. Returns one record per (sink, class, hash8),
        plus a single overflow record when the cap was hit since last drain."""
        with self._lock:
            counts, self._counts = self._counts, {}
            overflow, self._overflow = self._overflow, 0
        records = [
            RedactionHitRecord(sink=sink, cls=cls, hash8=digest, count=count)
            for (sink, cls, digest), count in counts.items()
        ]
        if overflow:
            records.append(
                RedactionHitRecord(
                    sink=OVERFLOW_SINK, cls=OVERFLOW_CLASS, hash8="0" * 8, count=overflow
                )
            )
        return records


__all__ = ["OVERFLOW_CLASS", "OVERFLOW_SINK", "RedactionHitRecord", "RedactionReporter"]
