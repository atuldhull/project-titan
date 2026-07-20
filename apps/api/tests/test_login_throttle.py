"""Login throttling logic (Phase 67; TITAN-SEC-017).

fakeredis, no DB — this exercises the counter arithmetic and both axes. The
integration test (test_auth_hardening_flow) drives it through the real endpoint.
Uses the repo's `run_async` helper rather than pytest-asyncio, matching the
rest of the suite.
"""

from __future__ import annotations

import fakeredis.aioredis

from tests.conftest import run_async
from titan.identity import throttle
from titan.identity.throttle import LoginThrottle


def _fresh() -> LoginThrottle:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return LoginThrottle(redis)


def test_below_threshold_is_always_allowed() -> None:
    t = _fresh()

    async def go() -> bool:
        for _ in range(throttle.THRESHOLD - 1):
            await t.record_failure("a@b.test", "1.2.3.4")
        return (await t.check("a@b.test", "1.2.3.4")).allowed

    assert run_async(go) is True


def test_the_fifth_failure_trips_backoff() -> None:
    t = _fresh()

    async def go() -> throttle.ThrottleDecision:
        for _ in range(throttle.THRESHOLD):
            await t.record_failure("a@b.test", "1.2.3.4")
        return await t.check("a@b.test", "1.2.3.4")

    decision = run_async(go)
    assert decision.allowed is False
    assert decision.retry_after_seconds > 0


def test_a_success_clears_the_backoff() -> None:
    """The property that keeps a legitimate user out of the throttle."""
    t = _fresh()

    async def go() -> tuple[bool, bool]:
        for _ in range(throttle.THRESHOLD):
            await t.record_failure("a@b.test", "1.2.3.4")
        before = (await t.check("a@b.test", "1.2.3.4")).allowed
        await t.clear("a@b.test", "1.2.3.4")
        after = (await t.check("a@b.test", "1.2.3.4")).allowed
        return before, after

    before, after = run_async(go)
    assert before is False
    assert after is True


def test_per_ip_backoff_sees_a_spray_across_accounts() -> None:
    """Per-account limiting alone is blind to this: each account fails once."""
    t = _fresh()

    async def go() -> throttle.ThrottleDecision:
        for i in range(throttle.THRESHOLD):
            await t.record_failure(f"victim{i}@b.test", "9.9.9.9")
        return await t.check("fresh@b.test", "9.9.9.9")

    decision = run_async(go)
    assert decision.allowed is False
    assert decision.triggered_by == "ip"


def test_per_account_backoff_follows_the_account_across_ips() -> None:
    t = _fresh()

    async def go() -> throttle.ThrottleDecision:
        for i in range(throttle.THRESHOLD):
            await t.record_failure("victim@b.test", f"10.0.0.{i}")
        return await t.check("victim@b.test", "10.0.0.250")

    decision = run_async(go)
    assert decision.allowed is False
    assert decision.triggered_by == "account"


def test_backoff_grows_exponentially() -> None:
    t = _fresh()

    async def go() -> tuple[float, float]:
        for _ in range(throttle.THRESHOLD):
            await t.record_failure("a@b.test", "1.2.3.4")
        first = (await t.check("a@b.test", "1.2.3.4")).retry_after_seconds
        await t.record_failure("a@b.test", "1.2.3.4")
        second = (await t.check("a@b.test", "1.2.3.4")).retry_after_seconds
        return first, second

    first, second = run_async(go)
    # One more failure roughly doubles the wait (allowing for the clock ticking).
    assert second > first


def test_email_key_is_case_insensitive() -> None:
    """Matches the CITEXT lookup: A@B and a@b are one account."""
    t = _fresh()

    async def go() -> throttle.ThrottleDecision:
        for _ in range(throttle.THRESHOLD):
            await t.record_failure("Victim@B.test", "1.2.3.4")
        return await t.check("victim@b.test", "5.5.5.5")

    decision = run_async(go)
    assert decision.allowed is False
    assert decision.triggered_by == "account"


def test_fails_open_when_redis_is_unavailable() -> None:
    """A Redis outage must not become a total auth outage."""

    class _BrokenRedis:
        async def hgetall(self, *_a: object, **_k: object) -> dict[str, str]:
            raise ConnectionError("redis down")

    t = LoginThrottle(_BrokenRedis())  # type: ignore[arg-type]

    async def go() -> bool:
        return (await t.check("a@b.test", "1.2.3.4")).allowed

    assert run_async(go) is True
