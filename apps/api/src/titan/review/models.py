"""The data the deterministic rubric reviewer consumes and produces.

Finding discipline (TITAN-WFL-033) is enforced by construction: a
:class:`ReviewFinding` cannot exist without a file, a line, a rubric ``rule_code``,
a message, and a concrete ``suggestion``. An ``advisory`` finding (outside the
diff's blast radius) is representable but is excluded from the blocking counts by
:func:`titan.review.reviewer.review_pr`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from titan.review.rubric import RUBRIC


def _has_segment(path: str, segment: str) -> bool:
    """Path-segment containment that is agnostic to a leading slash, so
    ``alembic/versions/x.py`` and ``a/alembic/versions/x.py`` both match
    ``alembic/versions``."""
    return f"/{segment}/" in f"/{path}/"


def is_test_path(path: str) -> bool:
    return _has_segment(path, "tests") or path.rsplit("/", 1)[-1].startswith("test_")


def is_migration_path(path: str) -> bool:
    return _has_segment(path, "migrations") or _has_segment(path, "alembic/versions")


def is_source_path(path: str) -> bool:
    """A Python source file that carries review obligations (not a test/migration)."""
    return path.endswith(".py") and not is_test_path(path) and not is_migration_path(path)


class ChangedFile(BaseModel):
    """A file in the PR, with its post-change content and added-line numbers."""

    model_config = {"frozen": True}

    path: str
    content: str
    added_lines: frozenset[int] = Field(default_factory=frozenset)

    @field_validator("path")
    @classmethod
    def _posix(cls, value: str) -> str:
        return value.replace("\\", "/")


class PullRequest(BaseModel):
    """The unit a review runs over: the changed files plus the surrounding facts a
    detector needs (whether an OpenAPI spec / ADR / test file also changed)."""

    model_config = {"frozen": True}

    files: tuple[ChangedFile, ...]
    openapi_changed: bool = False
    adr_paths: tuple[str, ...] = ()

    def changed_paths(self) -> frozenset[str]:
        return frozenset(f.path for f in self.files)

    def source_files(self) -> tuple[ChangedFile, ...]:
        """Python source files (excluding tests and migrations)."""
        return tuple(f for f in self.files if is_source_path(f.path))


class ReviewFinding(BaseModel):
    """One review finding, cited the way §6.6/§6.7 require. Immutable."""

    model_config = {"frozen": True}

    rule_code: str
    file: str
    line: int = Field(ge=0)
    message: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)
    advisory: bool = False

    @field_validator("rule_code")
    @classmethod
    def _known_rule(cls, value: str) -> str:
        if value not in RUBRIC:
            raise ValueError(f"unknown rule_code {value!r}; expected one of {sorted(RUBRIC)}")
        return value

    @property
    def severity(self) -> str:
        return RUBRIC[self.rule_code].severity

    @property
    def fingerprint(self) -> str:
        """Stable identity within a loop (the loop_findings UNIQUE key)."""
        return f"{self.rule_code}:{self.file}:{self.line}"


class ReviewResult(BaseModel):
    """The outcome of reviewing one PR."""

    model_config = {"frozen": True}

    verdict: str
    findings: tuple[ReviewFinding, ...]

    def blocking_findings(self) -> tuple[ReviewFinding, ...]:
        return tuple(f for f in self.findings if not f.advisory and f.severity == "blocking")

    def rule_codes(self) -> set[str]:
        return {f.rule_code for f in self.findings}


__all__ = [
    "ChangedFile",
    "PullRequest",
    "ReviewFinding",
    "ReviewResult",
    "is_migration_path",
    "is_source_path",
    "is_test_path",
]
