"""Gateway error types, surfaced as RFC 9457 problem+json (Vol 3 §5.2).

Reusing :class:`ProblemException` means the internal HTTP endpoint returns the
platform's one canonical error shape, and the in-process Python interface raises
the same typed errors the worker can catch.

:class:`UpstreamProviderError` carries a resilience ``category`` (§8.3) so the
gateway can decide retry-vs-fallback: ``timeout``/``server_error`` are retried
then fall over; ``rate_limit`` falls over immediately; ``context_overflow`` falls
over ONLY to a larger-context alias; ``client_error`` is not retried.
"""

from typing import Literal

import httpx

from titan.gateway.problems import ProblemException, problem_type

# Resilience categories driving the fallback decision (Vol 7 §8.3).
Category = Literal["timeout", "server_error", "rate_limit", "context_overflow", "client_error"]

# Substrings that mark a provider's "prompt too long" error across vendors.
_OVERFLOW_MARKERS = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "maximum number of tokens",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "exceeds the maximum",
)

# Categories worth retrying at the SAME provider before falling over.
RETRIABLE: frozenset[Category] = frozenset({"timeout", "server_error"})


class UnknownAliasError(ProblemException):
    """A caller supplied a ``model_alias`` (or agent route) that isn't defined —
    a client mistake, so 422."""

    def __init__(self, alias: str) -> None:
        super().__init__(
            status=422,
            title="Unknown model alias",
            detail=f"No model alias '{alias}' is defined in models.yaml.",
            problem_type=problem_type("validation"),
        )


class ProviderNotConfiguredError(ProblemException):
    """The resolved provider has no adapter yet, or is missing a required
    credential — a server misconfiguration, so 500."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            status=500,
            title="Provider not configured",
            detail=detail,
            problem_type=problem_type("internal"),
        )


class UpstreamProviderError(ProblemException):
    """The upstream provider failed (timeout, 5xx, rate limit, malformed body).
    502 — the gateway is a healthy proxy to an unhealthy dependency."""

    def __init__(
        self,
        provider: str,
        detail: str,
        *,
        status_code: int | None = None,
        category: Category = "server_error",
    ) -> None:
        super().__init__(
            status=502,
            title="Upstream provider error",
            detail=f"{provider}: {detail}",
            problem_type=problem_type("upstream"),
        )
        self.provider = provider
        self.status_code = status_code
        self.category = category


class AllProvidersDownError(ProblemException):
    """Every candidate in the fallback chain failed (or is circuit-open). Maps to
    the ``paused_provider`` outcome (TITAN-FND-053); 503 so callers retry later."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            status=503,
            title="All providers unavailable",
            detail=detail,
            problem_type=problem_type("upstream"),
        )


class BudgetExceededError(ProblemException):
    """A per-task/per-project budget cap would be breached and no local fallback
    is available — a hard stop (TITAN-OPS-021). 402 Payment Required."""

    def __init__(self, scope: str, detail: str) -> None:
        super().__init__(
            status=402,
            title="LLM budget exceeded",
            detail=detail,
            problem_type=problem_type("budget-exceeded"),
        )
        self.scope = scope


def _looks_like_overflow(body: str) -> bool:
    low = body.lower()
    return any(marker in low for marker in _OVERFLOW_MARKERS)


def classify_status(status: int, body: str) -> Category:
    """Map an upstream HTTP status + body to a resilience category (§8.3)."""
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server_error"
    if status == 400 and _looks_like_overflow(body):
        return "context_overflow"
    return "client_error"


def upstream_from_response(provider: str, resp: httpx.Response, body: str) -> UpstreamProviderError:
    """Build a categorized error from a non-2xx provider response."""
    return UpstreamProviderError(
        provider,
        f"{resp.status_code}: {body[:500]}",
        status_code=resp.status_code,
        category=classify_status(resp.status_code, body),
    )


def upstream_from_httpx(provider: str, exc: httpx.HTTPError) -> UpstreamProviderError:
    """Build a categorized error from a transport-level failure (timeout/connect)."""
    category: Category = "timeout" if isinstance(exc, httpx.TimeoutException) else "server_error"
    return UpstreamProviderError(provider, str(exc), category=category)


__all__ = [
    "AllProvidersDownError",
    "BudgetExceededError",
    "Category",
    "ProviderNotConfiguredError",
    "RETRIABLE",
    "UnknownAliasError",
    "UpstreamProviderError",
    "classify_status",
    "upstream_from_httpx",
    "upstream_from_response",
]
