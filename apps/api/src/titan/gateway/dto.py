"""Shared request-DTO helpers (gateway is cross-cutting infra, importable by
any module).
"""

from typing import Any, ClassVar

from pydantic import BaseModel, model_validator


class RejectNullFields(BaseModel):
    """Base for PATCH bodies whose optional fields map to NOT NULL columns.

    A field typed ``T | None = None`` is *optional* (may be omitted) — but
    Pydantic treats an explicitly-sent JSON ``null`` as a provided value, and
    with ``exclude_unset`` that null would reach the DB as ``SET col = NULL`` on
    a NOT NULL column → an IntegrityError → a 500 for plain client input.

    Subclasses list those columns in :attr:`non_nullable`; an explicit null for
    one is rejected here as a 422 (a ``ValueError`` in a before-validator
    surfaces as a request-validation problem), while genuinely-nullable fields
    (e.g. a milestone ``due_at`` a caller wants to clear) still accept null.
    """

    #: Field names that map to NOT NULL columns and must reject explicit null.
    non_nullable: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            nulled = sorted(f for f in cls.non_nullable if f in data and data[f] is None)
            if nulled:
                raise ValueError(f"field(s) may not be null: {', '.join(nulled)}")
        return data
