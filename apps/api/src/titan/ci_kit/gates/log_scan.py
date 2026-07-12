"""TITAN-WFL-029 — structured logging keeps secrets, tokens and PII out of logs.

§6.4 requires structured JSON logs and forbids logging secrets/tokens/PII,
enforced by "a redaction filter and a CI log-call scanner". The redaction filter
is a runtime processor shipped in the logging template; THIS is the CI scanner,
the belt to that braces: it AST-scans source for log calls that name a
secret/PII field, so a leak is caught at review time even if the runtime filter's
field list drifts.

Two checks, both low-false-positive by construction:
  1. A structured-log keyword whose NAME is a secret/PII field
     (logger.info("login", password=pw)) — the value never reaches the log
     redactor's allow path and must not be passed at all.
  2. ``print(...)`` — §6.4 mandates the structured logger; a print bypasses JSON,
     correlation, AND redaction. (Ruff T20 also catches this in the naming gate;
     here it is attributed to its real rule, WFL-029.)

The scanner runs over application source (the package root), NOT tests — a test
asserting on a redacted 'password' field is legitimate.
"""

import argparse
import ast
import sys
from pathlib import Path

from titan.ci_kit.config import CONFIG_FILENAME, ConfigError, load_config
from titan.ci_kit.gates.outcome import Finding, GateOutcome, failed, passed

GATE = "log_scan"
WFL_ID = "TITAN-WFL-029"

# Field names that must never be logged. Matched case-insensitively as a
# whole-word / snake-segment so `password` and `db_password` hit but a benign
# `password_reset_requested` event name does not (that is an event, checked
# separately — see _is_sensitive).
_SENSITIVE: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "private_key",
        "ssn",
        "credit_card",
        "card_number",
        "cvv",
        "pin",
        "otp",
    }
)

# Names commonly bound to a logger. A method call on one of these is a log call.
_LOGGER_NAMES: frozenset[str] = frozenset({"log", "logger", "logging", "_log", "_logger", "LOG"})
_LOG_METHODS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}
)

FIX_FIELD = (
    "never pass secrets/tokens/PII to a log call; log an id or a redacted marker "
    "(§6.4 TITAN-WFL-029)"
)
FIX_PRINT = (
    "use the structured logger, not print() — print bypasses JSON, correlation and "
    "redaction (§6.4 TITAN-WFL-029)"
)


def _is_sensitive(field: str) -> bool:
    """True if a keyword/field name denotes a secret or PII.

    A field is sensitive if any underscore-separated segment is a sensitive word,
    OR the whole name is. `db_password` -> segment 'password' hits;
    `password_changed` (an event flag) -> segment 'password' hits too, which is a
    conservative false-positive we accept (logging a field literally named
    'password_changed' is rare and easily renamed).
    """
    lowered = field.lower()
    if lowered in _SENSITIVE:
        return True
    return any(segment in _SENSITIVE for segment in lowered.split("_"))


def _dotted_tail(node: ast.expr) -> tuple[str, str]:
    """(receiver, attr) for an attribute call, else ('', '')."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
        return node.value.attr, node.attr
    return "", ""


def scan_source(source: str, path: str) -> list[Finding]:
    """Every logging violation in one module. Pure."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    findings: list[Finding] = []

    def add(line: int, message: str, fix: str) -> None:
        findings.append(Finding(wfl_id=WFL_ID, path=path, line=line, message=message, fix=fix))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # print(...) — a structured-logging bypass.
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            add(node.lineno, "print() bypasses the structured logger", FIX_PRINT)
            continue

        receiver, method = _dotted_tail(node.func)
        if receiver not in _LOGGER_NAMES or method not in _LOG_METHODS:
            continue
        for keyword in node.keywords:
            if keyword.arg and _is_sensitive(keyword.arg):
                add(
                    node.lineno,
                    f"log call passes a sensitive field {keyword.arg!r}",
                    FIX_FIELD,
                )

    return findings


def check(workspace: Path) -> GateOutcome:
    config = load_config(workspace)
    package_root = workspace / config.package_root
    if not package_root.is_dir():
        raise ConfigError(
            f"[titan-ci] package root {config.package_root} does not exist in the workspace"
        )

    findings: list[Finding] = []
    for path in sorted(package_root.rglob("*.py")):
        findings.extend(
            scan_source(path.read_text(encoding="utf-8"), path.relative_to(workspace).as_posix())
        )

    if findings:
        return failed(GATE, WFL_ID, tuple(findings))
    return passed(GATE, WFL_ID)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{WFL_ID}: no secrets/PII in logs")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help=f"workspace root (must contain {CONFIG_FILENAME})",
    )
    args = parser.parse_args(argv)
    try:
        outcome = check(args.workspace)
    except ConfigError as exc:
        print(f"ERROR {GATE} ({WFL_ID}): {exc}")
        return 2
    print(outcome.render())
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
