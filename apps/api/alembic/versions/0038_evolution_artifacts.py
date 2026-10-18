"""Evolution artifact ledger + prompt-SHA usage attribution (Phase 83).

``evolution_artifacts`` (Vol 10 §2, TITAN-EVO-002) is the immutable, queryable
record of approved evolution artifacts — Git commit SHA + repo path + content
hash + the approval signature (approved_by / approved_at / ep / diff_url). It
stores ONLY references, never prompt text (the bytes live in ``/evolution/**``
under git). It is synced from the manifest at startup and appended to by the
human-gated approve action. UNIQUE (path, content_sha256): one row per version.

``llm_usage.prompt_sha`` (Vol 10 §6.1, TITAN-EVO-015) records which approved
prompt version served each metered call, so any task is traceable to its exact
prompt. A nullable TEXT add — fast metadata-only ALTER (no table rewrite,
no backfill).

Hand-written per §6 (autogenerate forbidden).

Revision ID: 0038
Revises: 0037
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evolution_artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # prompt | prompt_base | routing | agent_spec | scoring | benchmark | ...
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("ref", sa.Text, nullable=False),  # agent_id / 'base' / 'routing'
        sa.Column("path", sa.Text, nullable=False),  # repo-relative POSIX path
        sa.Column("content_sha256", sa.Text, nullable=False),  # the prompt SHA
        sa.Column("commit_sha", sa.Text, nullable=True),  # approving HEAD
        sa.Column("semver", sa.Text, nullable=False, server_default=sa.text("'0.0.0'")),
        sa.Column("ep", sa.Text, nullable=False, server_default=sa.text("''")),
        # The EP gate (TITAN-EVO-001): a human user id + a signed timestamp.
        # Nullable so an in-flight (proposed, unapproved) artifact can be recorded.
        sa.Column("approved_by", UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", TIMESTAMP(timezone=True), nullable=True),
        sa.Column("diff_url", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.UniqueConstraint("path", "content_sha256", name="uq_evolution_artifacts_path_sha"),
    )
    op.create_index("ix_evolution_artifacts_kind_ref", "evolution_artifacts", ["kind", "ref"])
    op.execute(
        "CREATE TRIGGER trg_evolution_artifacts_updated_at BEFORE UPDATE ON evolution_artifacts "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    # TITAN-EVO-015: attribute each metered LLM call to its prompt version.
    op.add_column("llm_usage", sa.Column("prompt_sha", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("llm_usage", "prompt_sha")
    op.execute("DROP TRIGGER IF EXISTS trg_evolution_artifacts_updated_at ON evolution_artifacts")
    op.drop_index("ix_evolution_artifacts_kind_ref", table_name="evolution_artifacts")
    op.drop_table("evolution_artifacts")
