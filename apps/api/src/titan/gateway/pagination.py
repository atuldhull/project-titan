"""Cursor pagination (§4.1): ?limit=50&cursor=<opaque>, {"items", "next_cursor"}.

Cursors are opaque base64url-encoded JSON keyset positions. Lists are
enveloped; single resources are not.
"""

import base64
import json
from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import BaseModel

from titan.gateway.problems import ProblemException, problem_type

MAX_LIMIT = 200


class Page[T](BaseModel):
    """The §4.1 list envelope."""

    items: list[T]
    next_cursor: str | None = None


def encode_cursor(position: dict[str, Any]) -> str:
    raw = json.dumps(position, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    # ValueError covers binascii.Error and JSONDecodeError plus b64decode's
    # plain ValueError on non-ASCII input — tampering must be 400, never 500.
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProblemException(
            status=400,
            title="Invalid cursor",
            detail="The cursor is not a valid opaque pagination token.",
            problem_type=problem_type("invalid-cursor"),
        ) from exc
    if not isinstance(decoded, dict):
        raise ProblemException(
            status=400,
            title="Invalid cursor",
            detail="The cursor payload has the wrong shape.",
            problem_type=problem_type("invalid-cursor"),
        )
    return decoded


class PaginationParams:
    """FastAPI dependency: validated limit + decoded keyset position."""

    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
        cursor: Annotated[str | None, Query()] = None,
    ) -> None:
        self.limit = limit
        self.position: dict[str, Any] | None = decode_cursor(cursor) if cursor else None


Pagination = Annotated[PaginationParams, Depends(PaginationParams)]
