"""Identity tables: users, roles, user_roles (Vol 3 §6; owned per §3)."""

import uuid

from sqlalchemy import Boolean, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from titan.db import Base, ConventionsMixin, uuid7


class User(ConventionsMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("auth_provider", "auth_subject"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    email: Mapped[str] = mapped_column(CITEXT(), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 'oidc:google' | 'oidc:github' | 'local'
    auth_provider: Mapped[str] = mapped_column(Text, nullable=False)
    auth_subject: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Role(ConventionsMixin, Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    # owner|admin|maintainer|operator|viewer (§9.3)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # ["project:write","task:transition",...]
    permissions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)


class UserRole(ConventionsMixin, Base):
    """Role assignment; project_id NULL = org-wide (Vol 3 §6/§9.3).

    Spec deviation, documented: the spec DDL puts nullable project_id inside
    the PRIMARY KEY, which Postgres forbids. Same semantics here via a
    surrogate PK + UNIQUE NULLS NOT DISTINCT over the natural key, so a user
    gets at most one org-wide row and one row per project for a given role.
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "project_id", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
