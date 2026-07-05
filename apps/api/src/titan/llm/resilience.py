"""Gateway resilience primitives (Vol 7 §8.3, TITAN-FND-053).

A per-provider circuit breaker (open after 5 failures in 30 s, half-open probe
after a 30 s cooldown) and a retry helper with jittered exponential backoff for
the retriable categories (timeout / 5xx). Rate limits and context overflows are
NOT retried at the same provider — they fall over immediately.

Time and randomness are injectable so tests are fast and deterministic without
touching wall-clock behavior in production.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, TypeVar

from titan.llm.errors import RETRIABLE, UpstreamProviderError

T = TypeVar("T")

_State = Literal["closed", "open", "half_open"]


@dataclass
class _ProviderCircuit:
    failures: list[float] = field(default_factory=list)
    opened_at: float | None = None
    probe_in_flight: bool = False


class CircuitBreaker:
    """Per-provider breaker. ``allow`` gates a call; ``record_*`` updates state.

    Open after ``threshold`` failures within ``window`` seconds; stays open for
    ``cooldown`` seconds, then admits a single half-open probe whose outcome
    closes (success) or re-opens (failure) the circuit.
    """

    def __init__(
        self,
        *,
        threshold: int = 5,
        window: float = 30.0,
        cooldown: float = 30.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._window = window
        self._cooldown = cooldown
        self._now = now
        self._circuits: dict[str, _ProviderCircuit] = {}

    def _circuit(self, provider: str) -> _ProviderCircuit:
        return self._circuits.setdefault(provider, _ProviderCircuit())

    def state(self, provider: str) -> _State:
        circuit = self._circuit(provider)
        if circuit.opened_at is None:
            return "closed"
        if self._now() - circuit.opened_at >= self._cooldown:
            return "half_open"
        return "open"

    def allow(self, provider: str) -> bool:
        """True when a call may proceed: always when closed; in half-open admit a
        SINGLE probe (subsequent callers wait until it resolves)."""
        circuit = self._circuit(provider)
        state = self.state(provider)
        if state == "open":
            return False
        if state == "half_open":
            if circuit.probe_in_flight:
                return False
            circuit.probe_in_flight = True  # this caller is the probe
        return True

    def record_success(self, provider: str) -> None:
        self._circuits[provider] = _ProviderCircuit()

    def record_failure(self, provider: str) -> None:
        now = self._now()
        circuit = self._circuit(provider)
        if circuit.opened_at is not None:
            # A half-open probe failed → re-open the cooldown window.
            circuit.opened_at = now
            circuit.failures = []
            circuit.probe_in_flight = False  # next cooldown admits a fresh probe
            return
        circuit.failures = [t for t in circuit.failures if now - t < self._window]
        circuit.failures.append(now)
        if len(circuit.failures) >= self._threshold:
            circuit.opened_at = now


@dataclass
class RetryPolicy:
    """Retry the retriable categories with jittered exponential backoff."""

    max_retries: int = 2
    base_delay: float = 0.05
    max_delay: float = 2.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    rand: Callable[[], float] = random.random

    async def run(self, attempt: Callable[[], Awaitable[T]]) -> T:
        """Call ``attempt`` up to ``max_retries + 1`` times, backing off between
        retries; only a retriable :class:`UpstreamProviderError` is retried."""
        last: UpstreamProviderError | None = None
        for n in range(self.max_retries + 1):
            try:
                return await attempt()
            except UpstreamProviderError as exc:
                last = exc
                if exc.category not in RETRIABLE or n == self.max_retries:
                    raise
                backoff = min(self.max_delay, self.base_delay * (2**n))
                await self.sleep(backoff * (0.5 + 0.5 * self.rand()))  # full-ish jitter
        assert last is not None  # unreachable: the loop returns or raises
        raise last


__all__ = ["CircuitBreaker", "RetryPolicy"]
