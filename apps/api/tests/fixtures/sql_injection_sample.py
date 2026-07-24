"""A DELIBERATELY VULNERABLE fixture for the TITAN-SEC-071 semgrep rule.

This file is NEVER imported or executed — it exists only so the SQL-injection
semgrep rule (.semgrep/sql-injection.yml) has a committed positive case to fire
on (the acceptance: "the semgrep rule fires on a committed string-SQL fixture").
Every function below builds SQL by string concatenation / %-formatting /
str.format() — the exact patterns the rule bans. `make semgrep` scans src/ and
alembic/ (which are clean); this fixture is scanned only by the rule's self-test.
"""

from sqlalchemy import text


def concat(conn, user_id):  # type: ignore[no-untyped-def]
    # nosec - intentionally vulnerable fixture
    return conn.execute(text("SELECT * FROM users WHERE id = " + user_id))


def percent_format(conn, name):  # type: ignore[no-untyped-def]
    return conn.execute(text("SELECT * FROM users WHERE name = '%s'" % name))


def str_format(conn, email):  # type: ignore[no-untyped-def]
    return conn.execute(text(f"SELECT * FROM users WHERE email = '{email}'"))


def migration_concat(op, table):  # type: ignore[no-untyped-def]
    op.execute("DROP TABLE " + table)


def fstring_subscript(conn, request):  # type: ignore[no-untyped-def]
    # The dominant modern vector: request data spliced into an f-string SQL.
    return conn.execute(text(f"SELECT * FROM users WHERE id = {request.args['id']}"))


def fstring_call(conn):  # type: ignore[no-untyped-def]
    return conn.execute(text(f"SELECT * FROM users WHERE id = {get_user_input()}"))


def get_user_input():  # type: ignore[no-untyped-def]
    return "1 OR 1=1"
