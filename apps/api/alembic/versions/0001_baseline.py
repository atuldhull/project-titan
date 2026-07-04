"""Baseline — empty schema anchor; the Vol 3 §6 tables arrive in Phase 6.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
