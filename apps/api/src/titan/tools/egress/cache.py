"""URL-keyed response cache for the fetch-gateway (Phase 23).

The gateway makes the outbound HTTPS call server-side, so it holds the plaintext
response and can cache it with NO TLS interception — this is what makes 'a
repeated web.fetch is a cache hit' work for https:// pages. An in-process LRU +
TTL with hit/miss accounting; a Redis-backed store can replace it behind the same
tiny surface when the gateway scales out."""

import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class CachedResponse:
    status: int
    content_type: str
    body: bytes
    final_url: str


class ResponseCache:
    """Bounded LRU with per-entry TTL; counts hits/misses for observability + tests."""

    def __init__(self, *, max_entries: int = 512, ttl_seconds: float = 900.0) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._store: OrderedDict[str, tuple[float, CachedResponse]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str, *, now: float | None = None) -> CachedResponse | None:
        clock = time.monotonic() if now is None else now
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, response = entry
        if clock >= expires_at:
            del self._store[key]
            self.misses += 1
            return None
        self._store.move_to_end(key)  # LRU touch
        self.hits += 1
        return response

    def put(self, key: str, response: CachedResponse, *, now: float | None = None) -> None:
        clock = time.monotonic() if now is None else now
        self._store[key] = (clock + self._ttl, response)
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        self._store.clear()


__all__ = ["CachedResponse", "ResponseCache"]
