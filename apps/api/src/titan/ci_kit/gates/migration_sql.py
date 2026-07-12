"""TITAN-WFL-027 — the §6.2 database naming row, enforced on migrations.

The §6.2 table fixes: ``snake_case``; plural tables; ``id`` PK; ``<table>_id``
FKs; indexes named ``ix_<table>_<cols>``. Migrations are the only place a schema
name is minted, so that is where the rule is cheapest to enforce — once a table
ships, renaming it costs a migration and a deploy.

This is a linter, not a SQL parser: it reads DDL statements with regexes over a
comment- and string-stripped copy of the file. That is a deliberate ceiling. It
will not understand a table name assembled at runtime by a Python migration, and
it does not try to — it checks the literal DDL that the overwhelming majority of
migrations are made of, and stays silent rather than guessing on the rest.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from titan.ci_kit.config import CONFIG_FILENAME, load_config
from titan.ci_kit.gates.outcome import Finding, GateOutcome, failed, passed, skipped

GATE = "migration_sql"
WFL_ID = "TITAN-WFL-027"

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")

_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"']?(?:\w+\.)?(?P<name>\w+)[\"']?",
    re.IGNORECASE,
)
_CREATE_INDEX = re.compile(
    r"create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?"
    r"[\"']?(?P<name>\w+)[\"']?\s+on\s+[\"']?(?:\w+\.)?(?P<table>\w+)[\"']?",
    re.IGNORECASE,
)
_REFERENCES = re.compile(
    r"[\"']?(?P<column>\w+)[\"']?\s+[^,()]*?references\s+[\"']?(?:\w+\.)?(?P<table>\w+)[\"']?",
    re.IGNORECASE,
)
# A Postgres dollar-quote opening tag: $$ or $identifier$.
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")

# Irregular plurals we must not "correct". English pluralisation is not
# decidable by rule, so the gate only rejects the forms it is CERTAIN are
# singular (see _looks_singular) rather than trying to prove plurality.
_ALWAYS_PLURAL = frozenset({"data", "media", "criteria", "metadata", "schema_migrations"})


@dataclass(frozen=True)
class _Statement:
    line: int
    text: str


def strip_noise(sql: str) -> str:
    """Blank out comments and string literals, preserving line numbers.

    Without this the linter reports the ``CREATE TABLE`` inside a docstring in an
    Alembic migration, which is exactly the kind of false positive that gets a
    gate switched off.
    """
    out: list[str] = []
    i, n = 0, len(sql)

    def blank_run(start: int, end: int) -> None:
        """Append end-start blanks, preserving newlines so line numbers hold."""
        for j in range(start, min(end, n)):
            out.append("\n" if sql[j] == "\n" else " ")

    while i < n:
        two = sql[i : i + 2]
        if two == "--":
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
        elif two == "/*":
            while i < n and sql[i : i + 2] != "*/":
                out.append("\n" if sql[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
        elif sql[i] == "$" and (tag := _dollar_tag(sql, i)) is not None:
            # Postgres dollar-quoting ($$...$$ / $tag$...$tag$): the body is
            # opaque and, crucially, quotes/dashes inside it are NOT string or
            # comment delimiters. Missing this desyncs the scanner on a lone
            # apostrophe in a function body and blanks the real DDL that follows.
            close = sql.find(tag, i + len(tag))
            body_end = close if close != -1 else n
            end = body_end + len(tag) if close != -1 else n
            blank_run(i, end)
            i = end
        elif sql[i] in "'\"":
            quote = sql[i]
            out.append(" ")
            i += 1
            while i < n and sql[i] != quote:
                out.append("\n" if sql[i] == "\n" else " ")
                i += 1
            out.append(" ")
            i += 1
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _dollar_tag(sql: str, i: int) -> str | None:
    """If a dollar-quote tag opens at ``i``, return it (e.g. ``$$`` or ``$fn$``).

    A tag is ``$``, an optional identifier, then ``$`` — and the leading char
    must not be part of an identifier (``a$b`` is not a dollar-quote), which the
    caller guarantees by only reaching here on a bare ``$``.
    """
    match = _DOLLAR_TAG.match(sql, i)
    return match.group(0) if match else None


def _looks_singular(table: str) -> bool:
    """True only when we are confident the name is singular.

    Conservative by construction: 'status' and 'address' end in 's' and are
    singular, but rejecting them would be worse than missing them.
    """
    if table in _ALWAYS_PLURAL:
        return False
    # An -s/-ch/-sh/-x/-z ending is either already plural or a form we refuse to
    # guess about ('status', 'address'), so we do not call it singular.
    last = table.rsplit("_", 1)[-1]
    return not last.endswith(("s", "ch", "sh", "x", "z"))


def _table_statements(sql: str) -> list[tuple[int, str, str]]:
    """(line, table, body) for each CREATE TABLE, body = the column list."""
    found: list[tuple[int, str, str]] = []
    for match in _CREATE_TABLE.finditer(sql):
        start = sql.find("(", match.end())
        if start == -1:
            continue
        depth, i = 0, start
        while i < len(sql):
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        line = sql.count("\n", 0, match.start()) + 1
        found.append((line, match.group("name"), sql[start + 1 : i]))
    return found


def lint_sql(sql: str, path: str) -> list[Finding]:
    """Every §6.2 database-naming violation in one migration. Pure."""
    cleaned = strip_noise(sql)
    findings: list[Finding] = []

    def add(line: int, message: str, fix: str) -> None:
        findings.append(Finding(wfl_id=WFL_ID, path=path, line=line, message=message, fix=fix))

    for line, table, body in _table_statements(cleaned):
        if not SNAKE_CASE.match(table):
            add(
                line,
                f"table {table!r} is not snake_case",
                "rename the table to snake_case (§6.2)",
            )
        elif _looks_singular(table):
            add(
                line,
                f"table {table!r} is singular; §6.2 requires plural table names",
                f"rename the table to {table}s (§6.2)",
            )

        columns = [segment.strip() for segment in body.split(",")]
        names = [c.split()[0].strip('"') for c in columns if c.split()]
        if "id" not in names:
            add(
                line,
                f"table {table!r} has no 'id' column; §6.2 requires 'id' as the primary key",
                "add an 'id' primary key column (§6.2)",
            )

        for column, referenced in _REFERENCES.findall(body):
            expected = f"{referenced.rstrip('s')}_id"
            if column != expected and not column.endswith("_id"):
                add(
                    line,
                    f"foreign key {column!r} -> {referenced!r} should be named <table>_id",
                    f"rename the column to {expected!r} (§6.2)",
                )

    for match in _CREATE_INDEX.finditer(cleaned):
        name, table = match.group("name"), match.group("table")
        line = cleaned.count("\n", 0, match.start()) + 1
        if not name.startswith(f"ix_{table}_"):
            add(
                line,
                f"index {name!r} on {table!r} must be named ix_{table}_<cols>",
                f"rename the index to ix_{table}_<cols> (§6.2)",
            )

    return findings


def check(workspace: Path) -> GateOutcome:
    config = load_config(workspace)
    migrations = workspace / config.migrations
    if not migrations.is_dir():
        return skipped(GATE, WFL_ID, f"no migrations directory at {config.migrations}")

    findings: list[Finding] = []
    paths = sorted(p for ext in ("*.sql", "*.py") for p in migrations.rglob(ext))
    for path in paths:
        findings.extend(
            lint_sql(
                path.read_text(encoding="utf-8"),
                path.relative_to(workspace).as_posix(),
            )
        )

    if not paths:
        return skipped(GATE, WFL_ID, f"no migrations found under {config.migrations}")
    if findings:
        return failed(GATE, WFL_ID, tuple(findings))
    return passed(GATE, WFL_ID)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{WFL_ID}: §6.2 database naming")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help=f"workspace root (must contain {CONFIG_FILENAME})",
    )
    args = parser.parse_args(argv)
    outcome = check(args.workspace)
    print(outcome.render())
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
