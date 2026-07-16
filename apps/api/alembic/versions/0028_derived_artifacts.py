"""Auto-generated project artifacts (Phase 51; Vol 6 §5, TITAN-WFL-020).

The six derived project artifacts (roadmap, kanban, dependency_graph, release_plan,
risk_analysis, debt_report) are versioned views over the domain state — never
hand-maintained. Each regeneration that produces new content appends a version;
content is deterministic (stable ordering, no wall-clock in the body) so deleting
every row and regenerating reproduces byte-identical content.

Mutable per TITAN-BCK-008 (a regeneration bumps the row set, not a row), so the
convention columns + set_updated_at trigger apply. Keyed uniquely by
(project_id, kind, version).

Hand-written (autogenerate is forbidden).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: None = None
depends_on: None = None


def _conventions() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    ]


def upgrade() -> None:
    op.create_table(
        "derived_artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        # Monotonic per (project_id, kind); the current artifact is the max artifact_version.
        sa.Column("artifact_version", sa.Integer, nullable=False),
        sa.Column("content_md", sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column("content_json", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # sha256 over the deterministic content — the change detector + replay oracle.
        sa.Column("content_hash", sa.Text, nullable=False),
        *_conventions(),
        sa.UniqueConstraint(
            "project_id", "kind", "artifact_version", name="uq_derived_artifacts_pkv"
        ),
        sa.CheckConstraint(
            "kind IN ('roadmap', 'kanban', 'dependency_graph', 'release_plan', "
            "'risk_analysis', 'debt_report')",
            name="ck_derived_artifacts_kind",
        ),
    )
    op.create_index(
        "ix_derived_artifacts_project_kind", "derived_artifacts", ["project_id", "kind"]
    )
    op.execute(
        "CREATE TRIGGER trg_derived_artifacts_updated_at BEFORE UPDATE ON derived_artifacts "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_derived_artifacts_updated_at ON derived_artifacts")
    op.drop_index("ix_derived_artifacts_project_kind", table_name="derived_artifacts")
    op.drop_table("derived_artifacts")
