"""Self-hosted feature flags + ops kill switches (Phase 72, Vol 9 §5.3, TITAN-OPS-012).

A single ``feature_flags`` table is the source of truth (cached in Redis with a
10 s TTL, busted on write via pub/sub — the cache lives in the app, not here).
Types: ``boolean`` | ``percentage`` | ``allowlist``; classes: ``release`` (ship
dark, default OFF) | ``ops`` (kill switches) | ``experiment`` (percentage
rollouts). Scope is ``global`` | ``org`` | ``project`` — TITAN is single-org, so
``org`` collapses onto ``global`` in practice (the same singleton pattern the
budget tiers use), but the column is kept for the contract.

Statuses/enums are TEXT + CHECK constraints, never Postgres ENUM types — the
same convention as ``tasks.status`` (0003) so a new class/type never needs an
``ALTER TYPE`` in a blue/green window.

The three global kill switches (``agents.pause_all``, ``intake.read_only``,
``llm.provider_override``) are seeded here so they EXIST on a fresh deploy
without a seed step — TITAN-OPS-012 requires them to exist and take effect in
≤15 s. They ship OFF. Ops kill switches are permanent controls, so they carry no
expiry (the 90-day staleness warning targets release/experiment flags).

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0036
Revises: 0035
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The three OPS kill switches (TITAN-OPS-012) — (key, description).
_KILL_SWITCHES: tuple[tuple[str, str], ...] = (
    (
        "agents.pause_all",
        "Halt all new agent task dispatch across every worker (kill switch).",
    ),
    (
        "intake.read_only",
        "Reject new intake writes — the platform accepts reads only (kill switch).",
    ),
    (
        "llm.provider_override",
        "Force every LLM call to the provider/alias named in `value`, "
        "overriding routing (kill switch).",
    ),
)


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=sa.text("''")),
        # boolean | percentage | allowlist
        sa.Column("flag_type", sa.Text, nullable=False),
        # release | ops | experiment
        sa.Column("flag_class", sa.Text, nullable=False),
        # global | org | project (single-org: org≈global)
        sa.Column("scope", sa.Text, nullable=False, server_default=sa.text("'global'")),
        # The on/off for boolean/ops, and the master switch for percentage/allowlist
        # (a disabled flag evaluates false regardless of percentage/allowlist).
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        # 0..100 for the percentage type (deterministic hashed rollout).
        sa.Column("percentage", sa.SmallInteger, nullable=True),
        # Subjects (project/user ids or keys) for the allowlist type.
        sa.Column("allowlist", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        # Optional scalar payload — e.g. llm.provider_override's target alias.
        sa.Column("value", sa.Text, nullable=True),
        sa.Column("owner", sa.Text, nullable=False),
        # NULL = no expiry (ops kill switches are permanent). Release/experiment
        # flags SHOULD carry one; CI warns on flags older than 90 days.
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint(
            "flag_type IN ('boolean', 'percentage', 'allowlist')",
            name="ck_feature_flags_type",
        ),
        sa.CheckConstraint(
            "flag_class IN ('release', 'ops', 'experiment')",
            name="ck_feature_flags_class",
        ),
        sa.CheckConstraint(
            "scope IN ('global', 'org', 'project')",
            name="ck_feature_flags_scope",
        ),
        sa.CheckConstraint(
            "percentage IS NULL OR (percentage >= 0 AND percentage <= 100)",
            name="ck_feature_flags_percentage_range",
        ),
        # A percentage flag MUST carry a percentage; the others must not need one.
        sa.CheckConstraint(
            "flag_type <> 'percentage' OR percentage IS NOT NULL",
            name="ck_feature_flags_percentage_present",
        ),
    )
    op.create_index("uq_feature_flags_key", "feature_flags", ["key"], unique=True)
    op.execute(
        "CREATE TRIGGER trg_feature_flags_updated_at BEFORE UPDATE ON feature_flags "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    for key, description in _KILL_SWITCHES:
        op.execute(
            sa.text(
                "INSERT INTO feature_flags "
                "(id, key, description, flag_type, flag_class, scope, enabled, owner) "
                "VALUES (:id, :key, :description, 'boolean', 'ops', 'global', false, 'platform') "
                "ON CONFLICT DO NOTHING"
            ).bindparams(id=uuid.uuid4(), key=key, description=description)
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_feature_flags_updated_at ON feature_flags")
    op.drop_index("uq_feature_flags_key", table_name="feature_flags")
    op.drop_table("feature_flags")
