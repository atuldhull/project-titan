"""Shared request-DTO helpers (gateway is cross-cutting infra, importable by
any module).
"""

import json
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator, model_validator


def _decode_jsonb(value: Any) -> Any:
    """Normalize a jsonb column value to a Python object.

    SQLAlchemy's asyncpg dialect may hand back a jsonb column as either the
    decoded object or the raw JSON string depending on the registered codec, so
    response DTOs decode defensively before validation."""
    return json.loads(value) if isinstance(value, str) else value


#: A jsonb object field on a response DTO (decodes a str payload if needed).
JsonbDict = Annotated[dict[str, Any], BeforeValidator(_decode_jsonb)]


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
