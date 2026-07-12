"""Shared ESLint runner for the TypeScript half of the constitution.

ESLint enforces two different constitutional rules from one config
(``eslint.config.mjs``): ``boundaries/*`` is Clean Architecture layering
(TITAN-WFL-022), and ``camelcase`` is the §6.2 naming table (TITAN-WFL-027).
The layering and naming gates each run ESLint and keep the violations for their
own rule — so each fail-closes on its own concern.

Fail-closed is the whole point of centralizing this: a non-zero ESLint exit with
NO parseable violations means ESLint could not run (missing dependency, bad
config, crash), which is an ERROR — never a silent pass. Reading "exit 2, no
`boundaries/` lines" as "layering is clean" was a real fail-open this replaces.
"""

import re
import subprocess  # noqa: S404 — fixed argv, no shell
from dataclasses import dataclass
from pathlib import Path
from shutil import which

# `eslint --format unix`: "path:line:col: message [severity/rule-id]"
_UNIX = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):(?P<col>\d+): (?P<message>.*?)\s*\[(?P<tag>[^\]]+)\]$"
)


class EslintUnavailable(Exception):
    """ESLint could not run — the TS rules are unchecked, not satisfied."""


@dataclass(frozen=True)
class EslintViolation:
    path: str
    line: int
    rule: str
    message: str


def parse_eslint_unix(output: str) -> list[EslintViolation]:
    """Parse ``eslint --format unix`` output into violations. Pure."""
    violations: list[EslintViolation] = []
    for raw in output.splitlines():
        match = _UNIX.match(raw.strip())
        if match is None:
            continue
        tag = match.group("tag")
        # tag is "<severity>/<rule-id>" (the unix formatter always prefixes the
        # severity) or bare "<rule-id>". Strip ONLY the leading severity, because
        # a rule id itself contains slashes (e.g. "boundaries/element-types",
        # "@typescript-eslint/no-unused-vars") that must be preserved.
        rule = tag
        for severity in ("Error/", "Warning/"):
            if tag.startswith(severity):
                rule = tag[len(severity) :]
                break
        violations.append(
            EslintViolation(
                path=match.group("path").replace("\\", "/"),
                line=int(match.group("line")),
                rule=rule,
                message=match.group("message"),
            )
        )
    return violations


def run_eslint(workspace: Path, ts_src: str) -> list[EslintViolation]:
    """Run ESLint over ``ts_src`` and return every violation.

    Raises EslintUnavailable when ESLint exits non-zero WITHOUT producing any
    parseable violation — i.e. it failed to run rather than found problems.
    """
    if which("npx") is None:
        raise EslintUnavailable("npx is not on PATH but the workspace declares ts_src")

    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["npx", "--no-install", "eslint", ts_src, "--format", "unix"],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
    )
    if result.returncode == 0:
        return []
    violations = parse_eslint_unix(result.stdout)
    if not violations:
        # Non-zero exit with nothing to report = ESLint itself failed (missing
        # plugin, unresolved config, crash). Fail closed.
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise EslintUnavailable(
            "eslint exited non-zero without reporting a violation "
            f"(likely a missing dependency or config error): {detail[-1] if detail else '?'}"
        )
    return violations


__all__ = ["EslintUnavailable", "EslintViolation", "parse_eslint_unix", "run_eslint"]
