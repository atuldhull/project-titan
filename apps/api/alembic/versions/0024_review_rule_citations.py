"""Rule citations on loop findings (Phase 47; Vol 6 §6.6 R1-R11, TITAN-WFL-033).

Extend the Phase-44 ``loop_findings`` ledger — NOT a second store — so every
review finding records the §6.6 rule it cites and whether it is ``advisory``
(outside the diff's blast radius, and therefore never blocking). Existing rows
predate the rubric, so ``rule_code`` is nullable; new findings carry it.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.add_column("loop_findings", sa.Column("rule_code", sa.Text, nullable=True))
    op.add_column(
        "loop_findings",
        sa.Column("advisory", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    # A rule_code, when present, must be one of the eleven §6.6 codes.
    op.create_check_constraint(
        "ck_loop_findings_rule_code",
        "loop_findings",
        "rule_code IS NULL OR rule_code IN "
        "('R1','R2','R3','R4','R5','R6','R7','R8','R9','R10','R11')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_loop_findings_rule_code", "loop_findings", type_="check")
    op.drop_column("loop_findings", "advisory")
    op.drop_column("loop_findings", "rule_code")
