"""GitHub rate-limit token bucket fed from ``X-RateLimit-*`` response headers (Vol 7 §6.1,
TITAN-INT-040). Below 10% remaining, WRITE tools refuse pre-emptively with a retry_after so
the orchestrator reschedules instead of hammering the API into a hard 403/429."""

from dataclasses import dataclass

#: Refuse writes once the remaining budget drops below this fraction of the limit.
THROTTLE_FRACTION = 0.10


@dataclass
class RateBucket:
    """The connector's live view of GitHub's primary rate limit. Updated from every
    response's ``X-RateLimit-*`` headers; starts optimistic (unknown → allow)."""

    limit: int = 0
    remaining: int = -1  # -1 = unknown (no response seen yet) → do not throttle
    reset_epoch: int = 0

    def observe(self, headers: dict[str, str]) -> None:
        """Fold in ``X-RateLimit-{Limit,Remaining,Reset}`` from a response (case-insensitive)."""
        lower = {k.lower(): v for k, v in headers.items()}
        self.limit = _int(lower.get("x-ratelimit-limit"), self.limit)
        self.remaining = _int(lower.get("x-ratelimit-remaining"), self.remaining)
        self.reset_epoch = _int(lower.get("x-ratelimit-reset"), self.reset_epoch)

    def should_throttle(self) -> bool:
        """True once the remaining budget is under 10% of the limit (known budgets only)."""
        if self.remaining < 0 or self.limit <= 0:
            return False
        return self.remaining < max(1, int(self.limit * THROTTLE_FRACTION))

    def retry_after(self, *, now_epoch: int) -> int:
        """Seconds until the window resets — what a throttled write reports so the caller
        can reschedule rather than spin. At least 1s; never negative."""
        return max(1, self.reset_epoch - now_epoch) if self.reset_epoch else 60


def _int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default
