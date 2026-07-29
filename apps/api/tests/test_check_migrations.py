"""Phase 76 (Vol 9 §5.4, OPS-013): the migration-safety gate.

DoD: the live 37 migrations pass; and a DELIBERATELY breaking migration (trivial
downgrade, or an ACCESS EXCLUSIVE table rewrite) turns the gate RED. Pure — no DB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.check_migrations import VERSIONS_DIR, collect_errors

pytestmark: list[Any] = []


def _write(tmp: Path, name: str, body: str) -> Path:
    path = tmp / name
    path.write_text(body, encoding="utf-8")
    return path


_GOOD_DOWN = "def downgrade() -> None:\n    op.drop_table('t')\n"


def test_live_migrations_pass() -> None:
    files = [p for p in VERSIONS_DIR.glob("*.py") if p.name != "__init__.py"]
    assert len(files) >= 37
    assert collect_errors(files) == []


def test_trivial_downgrade_is_flagged(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "0099_x.py",
        "def upgrade() -> None:\n    pass\ndef downgrade() -> None:\n    pass\n",
    )
    errors = collect_errors([p])
    assert any("trivial" in e for e in errors), errors


def test_baseline_trivial_downgrade_is_exempt(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "0001_baseline.py",
        "def upgrade() -> None:\n    pass\ndef downgrade() -> None:\n    pass\n",
    )
    assert collect_errors([p]) == []


def test_missing_downgrade_is_flagged(tmp_path: Path) -> None:
    p = _write(tmp_path, "0099_x.py", "def upgrade() -> None:\n    pass\n")
    assert any("no downgrade" in e for e in collect_errors([p]))


def test_add_column_not_null_without_default_is_flagged(tmp_path: Path) -> None:
    body = (
        "def upgrade() -> None:\n"
        "    op.add_column('users', sa.Column('age', sa.Integer(), nullable=False))\n"
        f"{_GOOD_DOWN}"
    )
    assert any(
        "ACCESS EXCLUSIVE" in e for e in collect_errors([_write(tmp_path, "0099_x.py", body)])
    )


def test_add_column_not_null_WITH_server_default_is_ok(tmp_path: Path) -> None:
    body = (
        "def upgrade() -> None:\n"
        "    op.add_column('users', sa.Column('age', sa.Integer(), nullable=False, "
        "server_default='0'))\n"
        f"{_GOOD_DOWN}"
    )
    assert collect_errors([_write(tmp_path, "0099_x.py", body)]) == []


def test_alter_column_type_is_flagged(tmp_path: Path) -> None:
    body = (
        "def upgrade() -> None:\n"
        "    op.alter_column('users', 'age', type_=sa.BigInteger())\n"
        f"{_GOOD_DOWN}"
    )
    assert any(
        "ALTER COLUMN TYPE" in e for e in collect_errors([_write(tmp_path, "0099_x.py", body)])
    )


def test_raw_alter_table_rewrite_is_flagged(tmp_path: Path) -> None:
    body = (
        "def upgrade() -> None:\n"
        "    op.execute('ALTER TABLE users ALTER COLUMN age TYPE bigint')\n"
        f"{_GOOD_DOWN}"
    )
    assert any(
        "ACCESS EXCLUSIVE" in e for e in collect_errors([_write(tmp_path, "0099_x.py", body)])
    )


def test_allow_exclusive_escape_hatch(tmp_path: Path) -> None:
    body = (
        "# migrations: allow-exclusive maintenance window CR-123\n"
        "def upgrade() -> None:\n"
        "    op.execute('ALTER TABLE users ALTER COLUMN age TYPE bigint')\n"
        f"{_GOOD_DOWN}"
    )
    assert collect_errors([_write(tmp_path, "0099_x.py", body)]) == []


# --- review-fix rules (findings #1-#5): each unsafe pattern must be flagged --------


def test_create_index_on_preexisting_table_is_flagged(tmp_path: Path) -> None:
    body = "def upgrade() -> None:\n    op.create_index('ix', 'users', ['email'])\n" + _GOOD_DOWN
    assert any(
        "postgresql_concurrently" in e
        for e in collect_errors([_write(tmp_path, "0099_x.py", body)])
    )


def test_create_index_on_same_file_new_table_is_ok(tmp_path: Path) -> None:
    body = (
        "def upgrade() -> None:\n"
        "    op.create_table('t')\n"
        "    op.create_index('ix', 't', ['c'])\n" + _GOOD_DOWN
    )
    assert collect_errors([_write(tmp_path, "0099_x.py", body)]) == []


def test_create_index_concurrently_is_ok(tmp_path: Path) -> None:
    body = (
        "def upgrade() -> None:\n"
        "    op.create_index('ix', 'users', ['email'], postgresql_concurrently=True)\n" + _GOOD_DOWN
    )
    assert collect_errors([_write(tmp_path, "0099_x.py", body)]) == []


def test_rename_table_is_flagged(tmp_path: Path) -> None:
    body = "def upgrade() -> None:\n    op.rename_table('a', 'b')\n" + _GOOD_DOWN
    assert any("rename_table" in e for e in collect_errors([_write(tmp_path, "0099_x.py", body)]))


def test_alter_column_set_not_null_is_flagged(tmp_path: Path) -> None:
    body = (
        "def upgrade() -> None:\n    op.alter_column('users', 'age', nullable=False)\n" + _GOOD_DOWN
    )
    assert any("SET NOT NULL" in e for e in collect_errors([_write(tmp_path, "0099_x.py", body)]))


def test_alter_column_rename_is_flagged(tmp_path: Path) -> None:
    body = (
        "def upgrade() -> None:\n"
        "    op.alter_column('users', 'age', new_column_name='years')\n" + _GOOD_DOWN
    )
    assert any(
        "renames a column" in e for e in collect_errors([_write(tmp_path, "0099_x.py", body)])
    )


def test_execute_fstring_rewrite_is_flagged(tmp_path: Path) -> None:
    body = (
        "def upgrade() -> None:\n"
        "    t = 'users'\n"
        "    op.execute(f'ALTER TABLE {t} ALTER COLUMN age TYPE bigint')\n" + _GOOD_DOWN
    )
    assert any(
        "ACCESS EXCLUSIVE" in e for e in collect_errors([_write(tmp_path, "0099_x.py", body)])
    )


def test_execute_text_call_rewrite_is_flagged(tmp_path: Path) -> None:
    body = (
        "def upgrade() -> None:\n"
        "    op.execute(sa.text('ALTER TABLE users ALTER COLUMN age TYPE bigint'))\n" + _GOOD_DOWN
    )
    assert any(
        "ACCESS EXCLUSIVE" in e for e in collect_errors([_write(tmp_path, "0099_x.py", body)])
    )


def test_bare_return_downgrade_is_flagged(tmp_path: Path) -> None:
    body = "def upgrade() -> None:\n    op.drop_table('t')\ndef downgrade() -> None:\n    return\n"
    assert any("trivial" in e for e in collect_errors([_write(tmp_path, "0099_x.py", body)]))
