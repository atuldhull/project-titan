"""TITAN-WFL-025 — the repository pattern: no ORM outside ``infrastructure``.

§6.1 words the acceptance test as a "grep gate". This implements it as an AST
scan instead, which is strictly stronger for the same rule: grep fires on
``# don't import sqlalchemy here`` and on the string ``"sqlalchemy"`` in a
config dict, and misses nothing that the AST catches. A gate with false
positives gets disabled by the team it annoys, and a disabled gate enforces
nothing — so precision here is what keeps the rule alive.

What counts as a violation: any ``import``/``from`` of an ORM package from a
module NOT under ``<package>/infrastructure/``. The domain and application
layers may define repository *interfaces* (Protocols/ABCs); they may not touch
SQLAlchemy to do it.
"""

import argparse
import ast
import sys
from pathlib import Path

from titan.ci_kit.config import CONFIG_FILENAME, INFRASTRUCTURE_LAYER, ConfigError, load_config
from titan.ci_kit.gates.outcome import Finding, GateOutcome, failed, passed

GATE = "orm_boundary"
WFL_ID = "TITAN-WFL-025"

# §6.1 names SQLAlchemy explicitly; the sibling drivers are the same violation
# wearing a different name (importing asyncpg in a use case bypasses the
# repository just as thoroughly as importing sqlalchemy does).
ORM_PACKAGES: frozenset[str] = frozenset(
    {"sqlalchemy", "asyncpg", "psycopg", "psycopg2", "alembic", "sqlmodel"}
)

FIX = (
    "move the query behind a repository interface declared in application/ and "
    "implemented in infrastructure/ (§6.1 TITAN-WFL-025)"
)


def _root_package(module: str) -> str:
    return module.split(".", 1)[0]


def orm_imports(source: str) -> list[tuple[int, str]]:
    """Every ORM import in ``source`` as (line, module). Pure — the unit of this
    gate that the fixture tests pin directly.

    A file that does not parse is not this gate's problem: Ruff and the type
    checker both fail on a syntax error long before CI reaches here, and a gate
    that reports "you imported SQLAlchemy" because the file has an unbalanced
    paren would send the agent chasing the wrong rule.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _root_package(alias.name) in ORM_PACKAGES:
                    hits.append((node.lineno, alias.name))
        # `from . import x` has module=None, and a relative import (level > 0)
        # can never name a third-party ORM — so neither is checkable here.
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.level == 0
            and _root_package(node.module) in ORM_PACKAGES
        ):
            hits.append((node.lineno, node.module))
    return sorted(hits)


def check(workspace: Path) -> GateOutcome:
    config = load_config(workspace)
    package_root = workspace / config.package_root
    if not package_root.is_dir():
        raise ConfigError(
            f"[titan-ci] package root {config.package_root} does not exist in the workspace"
        )

    infrastructure = package_root / INFRASTRUCTURE_LAYER
    findings: list[Finding] = []

    for path in sorted(package_root.rglob("*.py")):
        if infrastructure in path.parents:
            continue
        for line, module in orm_imports(path.read_text(encoding="utf-8")):
            findings.append(
                Finding(
                    wfl_id=WFL_ID,
                    path=path.relative_to(workspace).as_posix(),
                    line=line,
                    message=(
                        f"{module!r} imported outside {INFRASTRUCTURE_LAYER}/ — "
                        "persistence access must go through a repository"
                    ),
                    fix=FIX,
                )
            )

    if findings:
        return failed(GATE, WFL_ID, tuple(findings))
    return passed(GATE, WFL_ID)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{WFL_ID}: {FIX}")
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
