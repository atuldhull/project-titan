"""Phase 76 (Vol 9 §5.4): the batched-backfill helper (bounded batches + sleeps)."""

from __future__ import annotations

from typing import Any

from titan.db import batched_backfill

pytestmark: list[Any] = []


class _FakeResult:
    def __init__(self, n: int) -> None:
        self.rowcount = n


class _FakeConn:
    def __init__(self, counts: list[int]) -> None:
        self._counts = list(counts)
        self.batches: list[int] = []

    def execute(self, _stmt: Any, params: dict[str, Any]) -> _FakeResult:
        self.batches.append(int(params["batch"]))
        return _FakeResult(self._counts.pop(0))


def test_backfill_loops_until_a_short_tail_batch() -> None:
    conn = _FakeConn([5000, 5000, 1234])  # two full batches, then the tail
    slept: list[float] = []
    total = batched_backfill(
        conn, "UPDATE t SET c=1 WHERE c IS NULL LIMIT :batch", sleep=slept.append
    )
    assert total == 5000 + 5000 + 1234
    assert conn.batches == [5000, 5000, 5000]  # ran 3 batches
    assert len(slept) == 2  # slept BETWEEN batches, never after the final short one


def test_backfill_single_short_batch_no_sleep() -> None:
    conn = _FakeConn([10])
    slept: list[float] = []
    total = batched_backfill(conn, "UPDATE ...", batch_size=5000, sleep=slept.append)
    assert total == 10 and conn.batches == [5000] and slept == []


def test_backfill_rejects_nonpositive_batch() -> None:
    try:
        batched_backfill(_FakeConn([]), "UPDATE ...", batch_size=0)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
