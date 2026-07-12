"""Constitution persistence: the append-only ``constitution_versions`` table
(Vol 6 §6). Private to the module — only ``service`` crosses the boundary."""

import uuid
from datetime import datetime

from sqlalchemy import Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, uuid7


class ConstitutionVersion(Base):
    """One immutable version of the engineering-standards constitution. Versions
    are append-only (a revision is a NEW row with the next ``version``, never an
    overwrite) so a gate ledger entry can name the exact version it judged against
    and that version can always be reproduced. An UPDATE/DELETE is rejected by a DB
    trigger (append-only)."""

    __tablename__ = "constitution_versions"
    __table_args__ = (UniqueConstraint("version", name="uq_constitution_versions_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    #: Monotonic version number; the active constitution is MAX(version).
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: The verbatim §6 markdown — the artifact agents retrieve into context.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: sha256 of ``body`` — lets a reviewer confirm the retrieved text is intact.
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    #: Provenance of this version (e.g. "Volume 6 §6").
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
