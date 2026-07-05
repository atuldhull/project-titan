"""Gateway error types, surfaced as RFC 9457 problem+json (Vol 3 §5.2).

Reusing :class:`ProblemException` means the internal HTTP endpoint returns the
platform's one canonical error shape, and the in-process Python interface raises
the same typed errors the worker can catch.
"""

from titan.gateway.problems import ProblemException, problem_type


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
    """The upstream provider failed (timeout, 5xx, malformed body). 502 — the
    gateway is a healthy proxy to an unhealthy dependency."""

    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(
            status=502,
            title="Upstream provider error",
            detail=f"{provider}: {detail}",
            problem_type=problem_type("upstream"),
        )


__all__ = [
    "ProviderNotConfiguredError",
    "UnknownAliasError",
    "UpstreamProviderError",
]
