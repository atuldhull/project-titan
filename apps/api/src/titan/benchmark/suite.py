"""The fixed 10-brief benchmark suite (Vol 1 §7, TITAN-FND-007/052, Phase 55).

The suite is the fixed corpus that backs the Section-7 KPI table: ten briefs of
graduated size, of which **exactly one** is marked ``executable`` and run
end-to-end per Phase-55 scope control; the other nine ship as fixtures only and
are wired into the nightly CI suite (never the per-commit pipeline).

The canonical suite is package data (:data:`DEFAULT_SUITE_RESOURCE`) so the runner
CLI and the tests load the identical corpus; :func:`load_suite` also accepts an
explicit path for a custom corpus.
"""

import json
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Size = Literal["tiny", "small", "medium"]

DEFAULT_SUITE_RESOURCE = "suite.json"
SUITE_SIZE = 10


class Brief(BaseModel):
    """One benchmark brief. ``local_only`` records whether the brief is expected to
    complete on the Ollama-only profile (the §7 "Local-only viability" KPI);
    ``executable`` marks the single brief run end-to-end this phase."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    size: Size = "tiny"
    local_only: bool = False
    executable: bool = False
    #: The line-coverage target the generated project's QA stage should meet — the
    #: fixture-declared expectation the run's measured coverage is checked against.
    expected_coverage: float = Field(default=0.6, ge=0.0, le=1.0)
    #: MoSCoW "must" feature ids the release must trace end-to-end.
    expected_features: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class BenchmarkSuite(BaseModel):
    """The full corpus. Enforces the fixed shape: exactly :data:`SUITE_SIZE`
    briefs, unique ids, and exactly one executable brief."""

    version: str = "1"
    briefs: list[Brief]

    @model_validator(mode="after")
    def _check_shape(self) -> "BenchmarkSuite":
        if len(self.briefs) != SUITE_SIZE:
            raise ValueError(f"the benchmark suite is fixed at {SUITE_SIZE} briefs")
        ids = [b.id for b in self.briefs]
        if len(set(ids)) != len(ids):
            raise ValueError("benchmark brief ids must be unique")
        executable = [b.id for b in self.briefs if b.executable]
        if len(executable) != 1:
            raise ValueError(
                f"exactly one brief must be executable this phase, found {len(executable)}"
            )
        return self

    @property
    def executable_brief(self) -> Brief:
        """The single brief run end-to-end (Phase-55 scope control)."""
        return next(b for b in self.briefs if b.executable)

    @property
    def local_only_count(self) -> int:
        """How many briefs are Ollama-viable — the numerator of the §7 KPI."""
        return sum(1 for b in self.briefs if b.local_only)

    def by_id(self, brief_id: str) -> Brief:
        for brief in self.briefs:
            if brief.id == brief_id:
                return brief
        raise KeyError(f"no benchmark brief with id {brief_id!r}")


def load_suite(path: str | Path | None = None) -> BenchmarkSuite:
    """Load the benchmark suite — the packaged canonical corpus by default, or a
    custom corpus from ``path``."""
    if path is not None:
        raw = Path(path).read_text(encoding="utf-8")
    else:
        raw = (
            resources.files("titan.benchmark.data")
            .joinpath(DEFAULT_SUITE_RESOURCE)
            .read_text(encoding="utf-8")
        )
    return BenchmarkSuite.model_validate(json.loads(raw))


__all__ = [
    "DEFAULT_SUITE_RESOURCE",
    "SUITE_SIZE",
    "BenchmarkSuite",
    "Brief",
    "Size",
    "load_suite",
]
