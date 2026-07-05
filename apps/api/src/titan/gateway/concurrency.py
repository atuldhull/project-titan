"""Optimistic concurrency via ETag / If-Match (§4.1, TITAN-BCK-008 version).

The ETag is the row's ``version`` column: ``"v3"``. Mutating updates MUST
send ``If-Match``; a missing header is 428 Precondition Required, a stale
one is 412 Precondition Failed.
"""

from fastapi import Request, Response

from titan.gateway.problems import ProblemException, problem_type


def version_conflict() -> ProblemException:
    """412 for a lost optimistic-lock race: the If-Match matched the version we
    read, but a concurrent commit moved it before our guarded UPDATE landed."""
    return ProblemException(
        status=412,
        title="Precondition failed",
        detail="The resource was modified concurrently; re-read and retry.",
        problem_type=problem_type("version-conflict"),
    )


def etag_for(version: int) -> str:
    return f'"v{version}"'


def set_etag(response: Response, version: int) -> None:
    response.headers["ETag"] = etag_for(version)


def require_if_match(request: Request, current_version: int) -> None:
    """Enforce §4.1 concurrency on a mutating route.

    RFC 9110 semantics: ``If-Match: *`` matches any current representation;
    the header may carry a comma-separated ETag list (weak prefixes are
    tolerated and compared by opaque value).
    """
    provided = request.headers.get("If-Match")
    if provided is None:
        raise ProblemException(
            status=428,
            title="Precondition required",
            detail="Mutating requests must send If-Match with the resource ETag.",
            problem_type=problem_type("precondition-required"),
        )
    candidates = [tag.strip() for tag in provided.split(",")]
    current = etag_for(current_version)
    matched = any(tag == "*" or tag.removeprefix("W/") == current for tag in candidates)
    if not matched:
        raise ProblemException(
            status=412,
            title="Precondition failed",
            detail=(
                f"Version conflict: resource is at {current}, request matched {provided.strip()}."
            ),
            problem_type=problem_type("version-conflict"),
        )
