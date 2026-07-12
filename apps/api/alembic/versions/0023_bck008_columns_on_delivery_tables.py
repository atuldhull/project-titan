"""Bring the Phase 41-44 delivery tables into TITAN-BCK-008 compliance.

TITAN-BCK-008 (MUST, Vol 3 §6): *every* table carries ``created_at``,
``updated_at`` (trigger-maintained), and an optimistic-lock ``version int NOT
NULL DEFAULT 1``. The Phase 6 core and every table through Phase 32 honor this —
including the append-only ledgers (``tool_audit``, ``event_inbox``,
``decisions``), which carry all three columns and simply omit the
``set_updated_at`` trigger so ``updated_at`` stays equal to ``created_at``.

Migrations 0019-0022 (pipeline, handoffs, constitution/clarification, loop)
added six tables that omitted some of these columns. This migration retrofits
them, following the established pattern:

  * append-only tables (a ``*_append_only`` reject trigger already blocks UPDATE)
    get the columns WITHOUT a ``set_updated_at`` trigger — matching tool_audit;
  * the one mutable table (``handoff_artifacts``) gets ``updated_at`` plus the
    reusable ``set_updated_at`` trigger.

The columns default (``now()`` / ``1``), so existing rows and existing INSERTs
(which name their columns explicitly — no ``SELECT *`` reads these tables) are
unaffected. Discovered by the ``-m schema`` census
(tests/test_schema.py::test_all_fifteen_tables_exist), which only runs when
TITAN_TEST_DATABASE_URL is set and had been skipped since Phase 41.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: None = None
depends_on: None = None

_TS = sa.TIMESTAMP(timezone=True)

# (table, needs_created_at, needs_updated_at, needs_version). All six are
# append-only EXCEPT handoff_artifacts.
_APPEND_ONLY: tuple[tuple[str, bool, bool, bool], ...] = (
    ("pipeline_stage_events", True, True, True),
    ("gate_evaluations", False, True, True),
    ("constitution_versions", False, True, False),
    ("loop_cycles", False, True, True),
    ("loop_stage_events", True, True, True),
)
# Mutable: gets the set_updated_at trigger like every other mutable table.
_MUTABLE_TABLE = "handoff_artifacts"


def _add_convention_columns(table: str, *, created: bool, updated: bool, version: bool) -> None:
    if created:
        op.add_column(
            table,
            sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
        )
    if updated:
        op.add_column(
            table,
            sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
        )
    if version:
        op.add_column(
            table,
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        )


def upgrade() -> None:
    for table, created, updated, version in _APPEND_ONLY:
        _add_convention_columns(table, created=created, updated=updated, version=version)

    # handoff_artifacts already has created_at + version; it is mutable (no
    # append-only trigger), so it takes updated_at AND the maintaining trigger.
    _add_convention_columns(_MUTABLE_TABLE, created=False, updated=True, version=False)
    op.execute(
        f"CREATE TRIGGER trg_{_MUTABLE_TABLE}_updated_at BEFORE UPDATE ON {_MUTABLE_TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{_MUTABLE_TABLE}_updated_at ON {_MUTABLE_TABLE}")
    op.drop_column(_MUTABLE_TABLE, "updated_at")

    for table, created, updated, version in _APPEND_ONLY:
        if version:
            op.drop_column(table, "version")
        if updated:
            op.drop_column(table, "updated_at")
        if created:
            op.drop_column(table, "created_at")
