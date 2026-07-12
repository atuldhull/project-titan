"""The machine-checkable subset of the constitution, one :class:`Rule` per gate.

This module is the kit's traceability spine: every config the installer drops and
every gate the CI workflow runs resolves back to exactly one TITAN-WFL id here.
Vol 6 §6 remains the single source of truth — nothing in this file restates a
rule, it only names the mechanism that enforces one.

Not every constitutional rule is machine-checkable. TITAN-WFL-023 (SOLID),
-024 (DDD-where-appropriate) and -026 (CQRS-only-if-justified) require judgement
about *intent* and are enforced by the Code Reviewer Agent against §6.6, not
here. Listing them as :data:`ADVISORY_RULES` is deliberate: it keeps the boundary
between "the machine rejects this" and "the reviewer rejects this" explicit
rather than letting a reader assume a green CI run proves constitutional
compliance.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    """One machine-enforced constitutional rule and the gate that enforces it."""

    gate: str
    """Gate id — the key the runner and the CI workflow address it by."""

    wfl_id: str
    """The Vol 6 requirement this gate enforces. Never invent one."""

    title: str
    """What the gate rejects, in the constitution's own terms."""

    mechanism: str
    """The tool doing the checking, for the CI log and the reviewer's context."""


RULES: tuple[Rule, ...] = (
    Rule(
        gate="layering",
        wfl_id="TITAN-WFL-022",
        title="Clean Architecture layering: dependencies point inward only",
        mechanism="import-linter (Python) + eslint-plugin-boundaries (TypeScript)",
    ),
    Rule(
        gate="orm_boundary",
        wfl_id="TITAN-WFL-025",
        title="Repository pattern: no ORM access outside the infrastructure layer",
        mechanism="AST scan for sqlalchemy imports outside infrastructure/",
    ),
    Rule(
        gate="naming",
        wfl_id="TITAN-WFL-027",
        title="Machine-enforced naming conventions (§6.2 table)",
        mechanism="Ruff pep8-naming (N) + ESLint naming rules",
    ),
    Rule(
        gate="migration_sql",
        wfl_id="TITAN-WFL-027",
        title=(
            "Database naming: snake_case, plural tables, id PK, <table>_id FKs, "
            "ix_<table>_<cols> indexes"
        ),
        mechanism="SQL migration linter",
    ),
    Rule(
        gate="commit_msg",
        wfl_id="TITAN-WFL-027",
        title="Conventional Commits",
        mechanism="commit-msg git hook",
    ),
    Rule(
        gate="diff_coverage",
        wfl_id="TITAN-WFL-030",
        title=(
            "Coverage gates on the diff: >=80% line / >=70% branch on changed code, "
            "never decreasing project-wide"
        ),
        mechanism="coverage.xml + git diff",
    ),
)

BY_GATE: dict[str, Rule] = {rule.gate: rule for rule in RULES}

ADVISORY_RULES: tuple[tuple[str, str], ...] = (
    ("TITAN-WFL-023", "SOLID — judged at module level by the reviewer (§6.6 R1)"),
    ("TITAN-WFL-024", "DDD where appropriate — the Architect classifies domain_complexity"),
    ("TITAN-WFL-026", "CQRS only if justified — an unlinked commands//queries/ split is §6.6 R6"),
)

__all__ = ["ADVISORY_RULES", "BY_GATE", "RULES", "Rule"]
