"""Identity tables (Vol 3 §6 + §3 ownership): users, roles, user_roles,
local_credentials, refresh_tokens, api_keys."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TIMESTAMP, UUID
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


class LocalCredential(ConventionsMixin, Base):
    """AUTH_MODE=local password (ADR-010 MVP half). Argon2id hash only
    (TITAN-SEC-017); kept out of the spec-exact users table on purpose —
    OIDC users never have one."""

    __tablename__ = "local_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)


class RefreshToken(ConventionsMixin, Base):
    """Opaque single-use refresh tokens with rotation families
    (TITAN-SEC-012). Only the SHA-256 of the token is stored; presenting a
    consumed token revokes the whole family."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class ApiKey(ConventionsMixin, Base):
    """Agent/CI API keys (§9.2): prefix-identifiable titan_ak_…, SHA-256
    hash at rest only. §3 assigns the table to identity; §6 defines no DDL
    for it, so this follows the §6 conventions."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Plain uuid (agents table is owned by the agents module; no FK to keep
    # module seams clean — resolved at the service layer).
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # titan_ak_ + first 8 chars: enough to identify a key in UIs/logs
    prefix: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


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
