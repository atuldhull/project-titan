"""Login-failure throttling (Phase 67; TITAN-SEC-017).

Per-account AND per-IP exponential backoff after 5 failures. The spec fixes the
threshold ("after 5 failures") and that both axes are throttled; the backoff
*shape* is unspecified (Vol 8 says only "exponential"), so the constants below
are this implementation's, chosen and documented rather than borrowed.

**Both axes, and why both.** Per-account backoff stops an attacker guessing one
victim's password from anywhere. Per-IP backoff stops an attacker spraying one
password across many accounts from one place — which per-account limiting alone
never sees, because each account has only one failure. A login is refused when
EITHER bucket is in backoff, so neither attack shape gets more than 5 tries per
window.

**The shape.** A failure count per key, held in Redis with a rolling TTL. Below
the threshold, nothing is imposed. At and above it, the caller must wait

    delay = min(BASE * 2**(failures - THRESHOLD), MAX_DELAY)

seconds since the last failure — 1s, 2s, 4s, 8s, … capped at MAX_DELAY. The
count itself expires after COUNT_TTL of no failures, so a handful of fat-finger
typos hours apart never compound into a lockout. A SUCCESSFUL login clears both
keys immediately (see :func:`clear`), so the throttle only ever costs an
attacker, never a user who eventually remembers their password.

**Why not a fixed lockout.** A fixed N-minute lockout after 5 failures is a
denial-of-service handed to the attacker: spray 5 failures at every account and
the whole org is locked out at once. Exponential backoff degrades gracefully —
the legitimate user waits seconds, the attacker's throughput collapses — and it
is what TITAN-SEC-017 asks for.

**Fail-open on a Redis outage, deliberately.** If Redis is unreachable the
throttle cannot read its counters; blocking every login on an infrastructure
blip would turn a cache outage into a total auth outage. Argon2id verification
and the audit trail still stand, so a brief window of un-throttled login is the
safer failure. The outage is logged.

Redis keys carry the Phase-8 ``deny:`` sibling style: ``throttle:acct:{email}``
and ``throttle:ip:{ip}``. Email is lower-cased so the key matches the CITEXT
lookup; a raw IP string is used verbatim (already normalised upstream by
``gateway.client_ip``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

THRESHOLD = 5
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 300.0  # 5 min — an attacker's ceiling, not a user's lockout
COUNT_TTL_SECONDS = 900  # 15 min of quiet clears the count entirely


def _acct_key(email: str) -> str:
    return f"throttle:acct:{email.strip().lower()}"


def _ip_key(ip: str) -> str:
    return f"throttle:ip:{ip}"


def _retry_after(failures: int, last_failure_at: float, now: float) -> float:
    """Seconds still to wait, or 0 if the caller may attempt now."""
    if failures < THRESHOLD:
        return 0.0
    delay: float = min(
        BASE_DELAY_SECONDS * 2 ** (failures - THRESHOLD), MAX_DELAY_SECONDS
    )
    remaining = (last_failure_at + delay) - now
    return max(0.0, remaining)


@dataclass(frozen=True)
class ThrottleDecision:
    """Whether login may proceed, and if not, for how long and which axis."""

    allowed: bool
    retry_after_seconds: float = 0.0
    # "account" | "ip" | None — which bucket triggered, for the audit event.
    triggered_by: str | None = None


class LoginThrottle:
    """Redis-backed per-account/per-IP backoff. One instance per app."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def _state(self, key: str) -> tuple[int, float]:
        """(failure count, last-failure epoch seconds) for one key."""
        try:
            raw = await self._redis.hgetall(key)
        except Exception:  # pragma: no cover - exercised via the fail-open test
            logger.warning("throttle.redis_unavailable", key=key)
            return (0, 0.0)
        if not raw:
            return (0, 0.0)
        # fakeredis/redis with decode_responses=True return str keys/values.
        count = int(raw.get("count", raw.get(b"count", 0)) or 0)
        last = float(raw.get("last", raw.get(b"last", 0.0)) or 0.0)
        return (count, last)

    async def check(self, email: str, ip: str) -> ThrottleDecision:
        """Decide before a password is verified. Never mutates counters.

        Checked against BOTH buckets; the longer wait wins, and the axis that
        produced it is reported so the audit event says which defence fired.
        """
        now = time.time()
        acct_failures, acct_last = await self._state(_acct_key(email))
        ip_failures, ip_last = await self._state(_ip_key(ip))

        acct_wait = _retry_after(acct_failures, acct_last, now)
        ip_wait = _retry_after(ip_failures, ip_last, now)

        if acct_wait <= 0 and ip_wait <= 0:
            return ThrottleDecision(allowed=True)
        if acct_wait >= ip_wait:
            return ThrottleDecision(
                allowed=False, retry_after_seconds=acct_wait, triggered_by="account"
            )
        return ThrottleDecision(
            allowed=False, retry_after_seconds=ip_wait, triggered_by="ip"
        )

    async def _bump(self, key: str, now: float) -> None:
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.hincrby(key, "count", 1)
                pipe.hset(key, "last", now)
                pipe.expire(key, COUNT_TTL_SECONDS)
                await pipe.execute()
        except Exception:  # pragma: no cover - fail-open path
            logger.warning("throttle.redis_unavailable", key=key)

    async def record_failure(self, email: str, ip: str) -> None:
        """Count a failed attempt against both axes."""
        now = time.time()
        await self._bump(_acct_key(email), now)
        await self._bump(_ip_key(ip), now)

    async def clear(self, email: str, ip: str) -> None:
        """Wipe both counters — called on a SUCCESSFUL login.

        This is what keeps the throttle off a legitimate user: five wrong
        guesses then the right one leaves nothing behind. Only the account key
        is truly personal; the IP key is cleared too because a success from an
        address is strong evidence the address is not mid-spray.
        """
        try:
            await self._redis.delete(_acct_key(email), _ip_key(ip))
        except Exception:  # pragma: no cover - fail-open path
            logger.warning("throttle.redis_unavailable", key="clear")
