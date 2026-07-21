"""Auth hardening: security audit, TOTP MFA, org MFA policy (Phase 67).

Sources of truth: Vol 8 §3.2 (TITAN-SEC-015/017), §4.2 (MFA policy is
Owner+Admin), §10.2 (auth events are audited), and docs/authentication-grants.md.

Four tables, all owned by `titan.identity` (the independence contract forbids
that module from reaching the project-scoped `tool_audit` ledger, and these
events are org/user-scoped rather than project-scoped anyway):

- ``security_events`` — append-only auth audit (TITAN-SEC-017/§10.2). Carries
  the *attempted* email as text, not just a user FK, because the events that
  matter most are for accounts that do not exist (enumeration probes) or are
  not yet identified (a throttle trip before any lookup). No FK on email for
  the same reason. `user_id` is a nullable FK for the events where identity is
  known.
- ``mfa_credentials`` — one TOTP secret per user. `confirmed_at` gates
  activation: a secret exists from the moment enrollment starts, but MFA is not
  enforced until the user proves possession by verifying one code, so a
  half-finished enrollment never locks anyone out.
- ``mfa_recovery_codes`` — single-use codes, Argon2id-hashed at rest (they are
  low-entropy human-typable strings, unlike API keys, so they get the password
  path not SHA-256). `used_at` marks consumption; rows are kept for audit.
- ``org_security_policy`` — the per-org `mfa_required` flag (TITAN-SEC-015).
  Keyed by the org string, since TITAN is single-tenant at the table level
  (`settings.org`); a row is created lazily on first policy write.

Hand-written (autogenerate is forbidden); ConventionsMixin columns spelled out.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: None = None
depends_on: None = None


def _conventions() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    ]


def _updated_at_trigger(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def upgrade() -> None:
    op.create_table(
        "security_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # e.g. login.succeeded | login.failed | login.throttled | mfa.enrolled |
        # mfa.verified | mfa.failed | mfa.required_policy_changed
        sa.Column("event_type", sa.Text(), nullable=False),
        # Known only for some events; NO FK on email so an event can name an
        # account that does not exist (the enumeration-probe case).
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("source_ip", sa.Text(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        *_conventions(),
    )
    # The two axes an operator actually queries: "what happened to this account"
    # and "what is this address doing". Both are time-ordered reads.
    op.create_index("ix_security_events_email_time", "security_events", ["email", "created_at"])
    op.create_index("ix_security_events_ip_time", "security_events", ["source_ip", "created_at"])
    _updated_at_trigger("security_events")

    op.create_table(
        "mfa_credentials",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Base32 TOTP secret. Stored as-is: it is a shared secret by
        # construction (the authenticator app holds the same value), so hashing
        # it would break verification. Encryption at rest is a vault concern
        # (Phase 68), not this table's.
        sa.Column("secret", sa.Text(), nullable=False),
        # NULL until the user verifies one code; enforcement reads this, so a
        # started-but-unconfirmed enrollment never blocks a login.
        sa.Column("confirmed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        *_conventions(),
    )
    _updated_at_trigger("mfa_credentials")

    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Argon2id hash — recovery codes are low-entropy typable strings, so
        # they take the password path, not the SHA-256 used for API keys.
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        *_conventions(),
    )
    op.create_index("ix_mfa_recovery_codes_user", "mfa_recovery_codes", ["user_id"])
    _updated_at_trigger("mfa_recovery_codes")

    op.create_table(
        "org_security_policy",
        # Keyed by the org string (single-tenant at the table level).
        sa.Column("org", sa.Text(), primary_key=True),
        sa.Column(
            "mfa_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        *_conventions(),
    )
    _updated_at_trigger("org_security_policy")


def downgrade() -> None:
    for table in (
        "org_security_policy",
        "mfa_recovery_codes",
        "mfa_credentials",
        "security_events",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
        op.drop_table(table)
