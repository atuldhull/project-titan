"""Vault persistence: secret METADATA and the secret audit ledger (Phase 68).

Only metadata lives in Postgres — never a value (SEC-050/SEC-051). The value
lives in the age vault (in-memory, decrypted from ciphertext at boot). These
tables are owned by the leaf ``titan.vault`` module; they import only
``titan.db`` (infrastructure), never a domain module, so the import-linter
"shared libraries never import domain modules" contract holds.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class SecretMetadataRow(ConventionsMixin, Base):
    """The non-secret facts about one stored secret (TITAN-SEC-052).

    ``name`` is the reference every consumer uses (get-by-reference) and is
    unique across the single-tenant vault. ``rotated_at`` is the rotation clock
    — distinct from ``updated_at`` so a metadata edit (e.g. changing the owner)
    does not silently reset the 90-day rotation window.
    """

    __tablename__ = "secret_metadata"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)  # org | project | task
    scope_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SET NULL, not CASCADE: the metadata (and its audit trail) outlives the
    # human who created the secret.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rotation_days: Mapped[int] = mapped_column(nullable=False, default=90)
    rotated_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class SecretAuditRow(ConventionsMixin, Base):
    """Append-only secret-lifecycle + envelope-integrity audit (§10.2, SEC-020).

    Records create/rotate/delete/inject and dropped (tampered/forged) queue
    envelopes. NEVER a secret value — only a reference/digest (SEC-092). Sits
    beside ``security_events`` (auth audit) rather than inside it because the
    vault is a leaf and cannot import ``titan.identity``.
    """

    __tablename__ = "secret_audit"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    secret_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


__all__ = ["SecretAuditRow", "SecretMetadataRow"]
