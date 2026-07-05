"""Cursor pagination (§4.1): ?limit=50&cursor=<opaque>, {"items", "next_cursor"}.

Cursors are opaque base64url-encoded JSON keyset positions. Lists are
enveloped; single resources are not.
"""

import base64
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Protocol

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


class _KeysetRow(Protocol):
    """Any list row must expose the canonical keyset columns."""

    created_at: Any
    id: Any


@dataclass(frozen=True)
class Keyset:
    """Keyset (seek) pagination on the canonical ``(created_at, id)`` tuple.

    Every list endpoint sorts by ``created_at`` with ``id`` (a time-ordered
    UUIDv7) as the deterministic tie-breaker, so the compound key is strictly
    monotonic and cursors never skip or repeat a row across pages. Repositories
    inject :attr:`order_by` and :meth:`where` into their SQL; the router calls
    :meth:`paginate` on the fetched rows to split off the next cursor.
    """

    limit: int
    descending: bool
    position: dict[str, Any] | None

    @property
    def order_by(self) -> str:
        direction = "DESC" if self.descending else "ASC"
        return f"created_at {direction}, id {direction}"

    @property
    def fetch(self) -> int:
        """Fetch one extra row to detect whether a further page exists."""
        return self.limit + 1

    def where(self) -> tuple[str, dict[str, Any]]:
        """A ``(sql_fragment, params)`` pair; empty fragment on the first page.

        The fragment has no leading boolean operator — callers splice it with
        their own ``AND``/``WHERE`` so a status filter can precede it.
        """
        if not self.position:
            return "", {}
        # Bind real datetime/UUID objects, not the cursor's JSON strings:
        # asyncpg infers each parameter's type from the CAST and rejects a str
        # where a timestamptz/uuid is expected. A cursor that survived base64
        # decode but carries a malformed keyset is a 400, never a 500.
        try:
            created_at = datetime.fromisoformat(self.position["created_at"])
            row_id = uuid.UUID(str(self.position["id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProblemException(
                status=400,
                title="Invalid cursor",
                detail="The cursor payload is not a valid pagination position.",
                problem_type=problem_type("invalid-cursor"),
            ) from exc
        operator = "<" if self.descending else ">"
        fragment = (
            f"(created_at, id) {operator} "
            "(CAST(:ks_created_at AS timestamptz), CAST(:ks_id AS uuid))"
        )
        return fragment, {"ks_created_at": created_at, "ks_id": row_id}

    def paginate[R: _KeysetRow](self, rows: Sequence[R]) -> tuple[list[R], str | None]:
        """Trim the sentinel row and, if present, emit the next cursor.

        The scan direction is baked into the cursor so a follow-up page can
        never diverge from the page that produced it — even if the client drops
        or flips the ``sort`` query param (see :func:`keyset_from`)."""
        page = list(rows[: self.limit])
        if len(rows) <= self.limit:
            return page, None
        last = page[-1]
        cursor = encode_cursor(
            {"created_at": last.created_at.isoformat(), "id": str(last.id), "desc": self.descending}
        )
        return page, cursor


def keyset_from(pagination: PaginationParams, *, descending: bool = True) -> Keyset:
    """Adapt the validated :class:`PaginationParams` into a :class:`Keyset`.

    ``descending`` supplies the FIRST page's direction (from the endpoint's
    ``sort`` param). On subsequent pages the cursor carries the authoritative
    direction, which wins — so a client that drops or flips ``sort`` mid-scan
    still gets a consistent, non-skipping sequence."""
    position = pagination.position
    if position is not None and "desc" in position:
        descending = bool(position["desc"])
    return Keyset(limit=pagination.limit, descending=descending, position=position)
