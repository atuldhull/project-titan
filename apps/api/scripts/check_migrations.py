"""CI migration-safety gate (Phase 76, Vol 9 §5.4, TITAN-OPS-013).

Every Alembic revision under ``alembic/versions/`` is statically checked so a
release can never ship a migration that (a) has no working ``downgrade()`` (CI must
be able to test the down-path, §5.4) or (b) takes a long ``ACCESS EXCLUSIVE`` lock
that blocks the other blue/green colour sharing the database. The banned rewrites:

  * ``op.add_column(... nullable=False ...)`` with no ``server_default`` — a full
    table rewrite under ACCESS EXCLUSIVE (expand→migrate→contract: add nullable +
    backfill + set-not-null in a later release instead);
  * ``op.alter_column(... type_=...)`` (ALTER COLUMN TYPE rewrite), ``nullable=False``
    (SET NOT NULL + validation scan), or ``new_column_name=`` (a rename that breaks
    the other colour);
  * ``op.rename_table`` (breaks the other colour still querying the old name);
  * ``op.create_index`` on a PRE-EXISTING table without ``postgresql_concurrently``
    (a SHARE lock blocks all writes for the build) — indexing a table created in the
    same migration is fine (it is empty);
  * raw ``ALTER TABLE ... (ADD COLUMN ... NOT NULL | SET NOT NULL | ALTER COLUMN
    ... TYPE)`` in an ``op.execute`` — including f-string and ``sa.text(...)`` forms.

A sanctioned maintenance-window change escapes rule (b) with a
``# migrations: allow-exclusive <reason>`` comment in the file. The initial
baseline (0001) is exempt from the downgrade rule (there is nothing below it).

Pure filesystem + ast — no DB, no network. Exit 0 clean, 1 on any violation.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_ALLOW_EXCLUSIVE = re.compile(r"#\s*migrations:\s*allow-exclusive")
_BASELINE = "0001"
# Raw-SQL rewrites that grab ACCESS EXCLUSIVE (checked inside op.execute strings).
_RAW_EXCLUSIVE = re.compile(
    r"alter\s+table\s+.+?(add\s+column\s+.+?\bnot\s+null\b(?!\s+default)"
    r"|set\s+not\s+null|alter\s+column\s+.+?\btype\b)",
    re.IGNORECASE | re.DOTALL,
)


def _is_op_call(node: ast.AST) -> bool:
    """A call to ``op.<something>(...)`` — the schema-mutating work a downgrade does."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    )


def _downgrade_is_trivial(fn: ast.FunctionDef) -> bool:
    """A downgrade is a real down-path only if it actually calls ``op.*`` to reverse
    the schema. A body of pass / ... / raise / a bare return / a lone log line does
    NO schema work and is trivial (review #5 — a bare ``return`` slipped past the old
    pass/raise-only check)."""
    return not any(_is_op_call(n) for n in ast.walk(fn))


def _column_call(node: ast.AST) -> ast.Call | None:
    """The sa.Column(...) call inside an op.add_column(...) argument list."""
    for arg in getattr(node, "args", []):
        if isinstance(arg, ast.Call) and _callee_name(arg.func).endswith("Column"):
            return arg
    return None


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _kw(call: ast.Call, name: str) -> ast.keyword | None:
    return next((k for k in call.keywords if k.arg == name), None)


def _is_false(kw: ast.keyword | None) -> bool:
    return kw is not None and isinstance(kw.value, ast.Constant) and kw.value.value is False


def _first_str_arg(call: ast.Call) -> str | None:
    """The first positional string literal (e.g. the table name in create_table)."""
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        return None
    return None


def _created_tables(tree: ast.AST) -> set[str]:
    """Tables CREATED in this same migration — indexing them is safe (empty/new)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee_name(node.func) == "create_table":
            name = _first_str_arg(node)
            if name is not None:
                names.add(name)
    return names


def _static_sql(node: ast.AST) -> str | None:
    """The static SQL text of an execute() argument — a plain string, the literal
    parts of an f-string, or a text('...') call — so a raw rewrite can't hide behind
    an f-string or sa.text() (review #4)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # f-string: join the literal parts
        return "".join(
            p.value for p in node.values if isinstance(p, ast.Constant) and isinstance(p.value, str)
        )
    if isinstance(node, ast.Call) and _callee_name(node.func).endswith("text"):
        return _first_str_arg(node)
    return None


def _exclusive_violations(tree: ast.AST, rev: str, created: set[str]) -> list[str]:
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if name == "add_column":
            col = _column_call(node)
            if (
                col is not None
                and _is_false(_kw(col, "nullable"))
                and _kw(col, "server_default") is None
            ):
                errors.append(
                    f"{rev}: add_column nullable=False with no server_default rewrites the table "
                    "under ACCESS EXCLUSIVE — add nullable then backfill + set-not-null later "
                    "(expand→migrate→contract)"
                )
        elif name == "alter_column":
            if _kw(node, "type_") is not None:
                errors.append(
                    f"{rev}: alter_column type_= is an ALTER COLUMN TYPE rewrite (ACCESS EXCLUSIVE)"
                )
            if _is_false(_kw(node, "nullable")):
                errors.append(
                    f"{rev}: alter_column nullable=False issues SET NOT NULL (ACCESS EXCLUSIVE + a "
                    "full validation scan pre-PG12) — backfill + add a CHECK ... NOT VALID instead"
                )
            if _kw(node, "new_column_name") is not None:
                errors.append(
                    f"{rev}: alter_column new_column_name renames a column — the other blue/green "
                    "colour still selects the old name and breaks (OPS-013)"
                )
        elif name == "rename_table":
            errors.append(
                f"{rev}: rename_table breaks the other blue/green colour still querying the old "
                "name (OPS-013) — add the new table + backfill + dual-write instead"
            )
        elif name == "create_index":
            table: str | None = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                val = node.args[1].value
                table = val if isinstance(val, str) else None
            concurrently = _kw(node, "postgresql_concurrently")
            is_concurrent = concurrently is not None and not _is_false(concurrently)
            if table is not None and table not in created and not is_concurrent:
                errors.append(
                    f"{rev}: create_index on the pre-existing table '{table}' without "
                    "postgresql_concurrently takes a SHARE lock that blocks all writes for the "
                    "build — use postgresql_concurrently=True (outside a transaction)"
                )
        elif name == "execute":
            for arg in node.args:
                sql = _static_sql(arg)
                if sql is not None and _RAW_EXCLUSIVE.search(sql):
                    errors.append(
                        f"{rev}: raw ALTER TABLE takes ACCESS EXCLUSIVE (§5.4 forbids >2s)"
                    )
    return errors


def collect_errors(files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in sorted(files):
        rev = path.stem
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            errors.append(f"{rev}: unparseable migration ({exc})")
            continue

        down = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "downgrade"),
            None,
        )
        if down is None:
            errors.append(f"{rev}: no downgrade() function (§5.4 requires a tested down-path)")
        elif not rev.startswith(_BASELINE) and _downgrade_is_trivial(down):
            errors.append(
                f"{rev}: downgrade() is trivial (pass / NotImplementedError) — write a real "
                "down-path or CI cannot test the rollback (§5.4)"
            )

        if not _ALLOW_EXCLUSIVE.search(text):
            errors.extend(_exclusive_violations(tree, rev, _created_tables(tree)))
    return errors


def main() -> None:
    files = [p for p in VERSIONS_DIR.glob("*.py") if p.name != "__init__.py"]
    if not files:
        raise SystemExit(f"check-migrations: no revisions found under {VERSIONS_DIR}")
    errors = collect_errors(files)
    if errors:
        sys.stderr.write("migration-safety check FAILED:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        raise SystemExit(1)
    sys.stdout.write(
        f"migration-safety OK: {len(files)} revisions, downgrades real, no unsafe locks.\n"
    )


if __name__ == "__main__":
    main()
