# migrations: allow-exclusive — pre-Phase-76 index on a small pre-existing table.
"""Index llm_usage by time, for the platform-wide cost query (Phase 63; Vol 4 §6.10).

Monitoring's default scope is the PLATFORM — every project plus the rows that
belong to none — over a range picked from 15m/1h/24h/7d. That query filters on
``created_at`` alone.

0007 gave ``llm_usage`` two indexes and neither serves it:
``idx_llm_usage_project`` leads with ``project_id``, so it only helps once a
project is pinned (the scope switcher's other setting), and ``idx_llm_usage_task``
leads with a column that has exactly one production writer and is NULL almost
everywhere. So the screen's default query — the one that runs on every load and
every range change — seq-scans the whole ledger.

A plain additive index: no column, constraint or data changes. Hand-written
(autogenerate is forbidden).
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: None = None
depends_on: None = None

_INDEX = "idx_llm_usage_created_at"


def upgrade() -> None:
    # DESC mirrors how the data is read — a range always ends at "now" and the
    # newest rows are the ones a 15m window wants — so the scan walks the index
    # from its leading edge rather than seeking into the middle of it.
    op.create_index(_INDEX, "llm_usage", [sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="llm_usage")
