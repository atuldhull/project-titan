"""Identity tables (Vol 3 §6 + §3 ownership): users, roles, user_roles,
local_credentials, refresh_tokens, api_keys, and the Phase-67 auth-hardening
tables (security_events, mfa_credentials, mfa_recovery_codes,
org_security_policy)."""

import uuid
from datetime import datetime
from typing import Any

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


class SecurityEvent(ConventionsMixin, Base):
    """Append-only auth audit (Phase 67; TITAN-SEC-017, §10.2).

    Carries the ATTEMPTED email as text, not only a user FK, because the events
    that matter most are for accounts that do not exist (enumeration probes) or
    are not yet identified (a throttle trip before any lookup). `user_id` is a
    nullable FK for the events where identity is known; deleting a user nulls it
    (SET NULL) rather than erasing the audit row.
    """

    __tablename__ = "security_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class MfaCredential(ConventionsMixin, Base):
    """One TOTP secret per user (Phase 67; TITAN-SEC-015).

    `confirmed_at` gates activation: the secret exists from the moment
    enrollment starts, but MFA is not enforced until the user proves possession
    by verifying one code, so a half-finished enrollment never locks anyone out.
    The base32 secret is stored as-is — it is a shared secret with the
    authenticator app, so hashing it would break verification; encryption at
    rest is a Phase-68 vault concern.
    """

    __tablename__ = "mfa_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    secret: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class MfaRecoveryCode(ConventionsMixin, Base):
    """Single-use MFA recovery codes, Argon2id-hashed (Phase 67).

    Recovery codes are low-entropy typable strings, so they take the password
    path, not the SHA-256 used for high-entropy API keys. `used_at` marks
    consumption; rows are kept for audit rather than deleted.
    """

    __tablename__ = "mfa_recovery_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class OrgSecurityPolicy(ConventionsMixin, Base):
    """Per-org security policy (Phase 67; TITAN-SEC-015).

    `mfa_required` forces MFA for Admin/Owner at their next privileged login.
    Keyed by the org string, since TITAN is single-tenant at the table level
    (`settings.org`); a row is created lazily on first policy write.
    """

    __tablename__ = "org_security_policy"

    org: Mapped[str] = mapped_column(Text, primary_key=True)
    mfa_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
