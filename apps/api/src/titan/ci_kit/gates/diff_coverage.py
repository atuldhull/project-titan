"""TITAN-WFL-030 — the coverage gates, enforced on the diff.

§6.5 states three separate obligations, and this gate enforces all three because
any one alone is trivially satisfiable while gaming the other two:

1. **>=80% line coverage of changed code.** Project-wide line coverage hides new
   untested code behind a large, well-tested existing codebase — a 500-line
   untested feature moves a 90%-covered 50k-line project by a fraction of a
   point.
2. **>=70% branch coverage of changed code.** Line coverage alone is satisfied by
   a test that executes an ``if`` and never takes the false edge.
3. **Project-wide coverage may never decrease.** Diff coverage alone is satisfied
   by deleting tests for code you did not touch.

"Changed code" means lines ADDED or MODIFIED relative to the merge-base with the
base ref — never deleted lines, which by definition have no coverage to measure.
"""

import argparse
import ast
import json
import re
import subprocess  # noqa: S404 — git is the only command run, with a fixed argv (no shell)
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from titan.ci_kit.config import (
    CONFIG_FILENAME,
    MIN_BRANCH_COVERAGE,
    MIN_LINE_COVERAGE,
    ConfigError,
    KitConfig,
    load_config,
)
from titan.ci_kit.gates.outcome import Finding, GateOutcome, failed, passed

GATE = "diff_coverage"
WFL_ID = "TITAN-WFL-030"

BASELINE_PATH = Path(".titan") / "ci" / "coverage-baseline.json"
COVERAGE_XML = "coverage.xml"

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_CONDITION = re.compile(r"\((?P<taken>\d+)/(?P<total>\d+)\)")


class CoverageError(Exception):
    """The gate could not run — a missing coverage.xml is a broken CI config, not
    a passing build. Raised, never returned as ``ok``, so the runner reports it
    as an ERROR rather than letting an absent report read as full coverage."""


@dataclass(frozen=True)
class LineCoverage:
    """Coverage of a single executable line, as Cobertura reports it."""

    hits: int
    branches_taken: int = 0
    branches_total: int = 0


@dataclass(frozen=True)
class DiffCoverage:
    """The measured result over the changed lines."""

    lines_total: int
    lines_covered: int
    branches_total: int
    branches_taken: int
    project_rate: float

    @property
    def line_rate(self) -> float:
        # No changed executable lines => nothing to cover => 100%, not 0%.
        # A docs-only PR must not fail the coverage gate.
        if self.lines_total == 0:
            return 100.0
        return 100.0 * self.lines_covered / self.lines_total

    @property
    def branch_rate(self) -> float:
        if self.branches_total == 0:
            return 100.0
        return 100.0 * self.branches_taken / self.branches_total


def parse_diff(diff: str) -> dict[str, set[int]]:
    """Added/modified line numbers per file, from ``git diff --unified=0``. Pure.

    A ``+++ `` line is treated as a file header ONLY in the pre-hunk region of a
    file (after ``diff --git`` and before the first ``@@``). With ``--unified=0``
    an added content line is ``+`` + content, so a line whose content begins with
    ``++ `` renders as ``+++ ``; reading that as a header would silently redirect
    every subsequent added line to a bogus file. Anchoring the header parse to
    ``diff --git`` boundaries closes that hole.
    """
    changed: dict[str, set[int]] = {}
    current: str | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = None
            in_hunk = False
            continue
        if not in_hunk and line.startswith("+++ "):
            target = line[4:].strip()
            # /dev/null is a deletion; it has no lines to cover.
            current = None if target == "/dev/null" else target.removeprefix("b/")
            continue
        if line.startswith("@@"):
            in_hunk = True
            match = _HUNK.match(line)
            if match and current is not None:
                start = int(match.group("start"))
                count = int(match.group("count") or "1")
                # count=0 marks a pure deletion hunk — no added lines.
                if count:
                    changed.setdefault(current, set()).update(range(start, start + count))
    return changed


def parse_coverage_xml(xml: str) -> tuple[dict[str, dict[int, LineCoverage]], float]:
    """Cobertura XML -> (per-file line coverage, project-wide line rate %). Pure.

    Cobertura records each ``<class>`` filename RELATIVE to a ``<sources>/<source>``
    root, not from the repo root. coverage.py with ``[tool.coverage.run] source =
    ["src/pkg"]`` (which real projects set, and the Phase-46 scaffold ships) emits
    ``filename="application/x.py"`` under ``<source>src/pkg</source>`` — so keying
    only on the bare filename never matches a git-diff path like
    ``src/pkg/application/x.py``. We reconstruct the full path by prefixing each
    source root, and fall back to a suffix match for absolute source roots.
    """
    try:
        root = ET.fromstring(xml)  # noqa: S314 — CI-generated report, not untrusted input
    except ET.ParseError as exc:
        raise CoverageError(f"{COVERAGE_XML} is not valid XML: {exc}") from exc

    sources: list[str] = []
    for source_node in root.iter("source"):
        text = (source_node.text or "").strip().replace("\\", "/").rstrip("/")
        if text and text != ".":
            sources.append(text)

    files: dict[str, dict[int, LineCoverage]] = {}
    for class_node in root.iter("class"):
        filename = class_node.get("filename")
        if not filename:
            continue
        # Cobertura writes the OS-native separator; normalise so a report
        # generated on Windows still matches git's forward-slash paths.
        filename = filename.replace("\\", "/")
        lines: dict[int, LineCoverage] = {}
        for line_node in class_node.iter("line"):
            number = line_node.get("number")
            if number is None:
                continue
            taken = total = 0
            if line_node.get("branch") == "true":
                condition = _CONDITION.search(line_node.get("condition-coverage", ""))
                if condition:
                    taken = int(condition.group("taken"))
                    total = int(condition.group("total"))
            lines[int(number)] = LineCoverage(
                hits=int(line_node.get("hits", "0")),
                branches_taken=taken,
                branches_total=total,
            )
        # Key by the full-from-root path (source root + filename); with no
        # source roots the filename is already full-from-root (Phase-45 shape).
        keys = [f"{src}/{filename}" for src in sources] if sources else [filename]
        for key in keys:
            files[key] = lines

    rate = root.get("line-rate")
    return files, (100.0 * float(rate) if rate is not None else 0.0)


def _coverage_for(
    files: dict[str, dict[int, LineCoverage]], path: str
) -> dict[int, LineCoverage] | None:
    """The coverage for a git-diff path, tolerating an absolute source root.

    Exact match first (the relative-source case); otherwise a UNIQUE coverage key
    whose tail equals the diff path (the absolute-source case). Ambiguous suffix
    matches resolve to None rather than guess."""
    hit = files.get(path)
    if hit is not None:
        return hit
    candidates = [lines for key, lines in files.items() if key.endswith("/" + path)]
    return candidates[0] if len(candidates) == 1 else None


def _measure(
    changed: dict[str, set[int]],
    files: dict[str, dict[int, LineCoverage]],
    project_rate: float,
) -> DiffCoverage:
    lines_total = lines_covered = branches_total = branches_taken = 0
    for path, numbers in changed.items():
        coverage = _coverage_for(files, path)
        if coverage is None:
            # Not in the report. This is NOT automatically safe: a changed source
            # file absent from the report is untested new code, handled separately
            # by untested_source_files(). Here we only skip files that carry no
            # coverage obligation (tests, config, README) — measuring their lines
            # would fail every docs change at 0%.
            continue
        for number in sorted(numbers):
            line = coverage.get(number)
            if line is None:
                continue  # not an executable line (blank, comment, `else:`)
            lines_total += 1
            if line.hits > 0:
                lines_covered += 1
            branches_total += line.branches_total
            branches_taken += line.branches_taken

    return DiffCoverage(
        lines_total=lines_total,
        lines_covered=lines_covered,
        branches_total=branches_total,
        branches_taken=branches_taken,
        project_rate=project_rate,
    )


def measure(changed: dict[str, set[int]], xml: str) -> DiffCoverage:
    """Intersect the diff with the coverage report. Pure — the gate's core."""
    files, project_rate = parse_coverage_xml(xml)
    return _measure(changed, files, project_rate)


def _is_source_path(path: str, config: KitConfig) -> bool:
    """True if ``path`` is Python source the coverage gate is responsible for.

    Source = a ``.py`` file under the package root. Tests, migrations, configs,
    and anything outside the package carry no diff-coverage obligation.
    """
    if not path.endswith(".py"):
        return False
    package_root = config.package_root.as_posix() + "/"
    if not path.startswith(package_root):
        return False
    tail = path[len(package_root) :]
    basename = tail.rsplit("/", 1)[-1]
    return not (basename.startswith("test_") or basename == "conftest.py")


def _file_has_executable_code(source: str) -> bool:
    """True if the module has anything a test could execute.

    Guards against false-failing an empty ``__init__.py`` or a docstring-only
    module as "untested": a file with no executable statements is legitimately
    absent from a coverage report and has nothing to cover.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Ruff/mypy own syntax errors; assume there is code so we do not wave a
        # broken new file through as "nothing to cover".
        return True
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring / bare literal
        if isinstance(node, ast.Pass):
            continue
        return True
    return False


def untested_source_files(
    changed: dict[str, set[int]],
    files: dict[str, dict[int, LineCoverage]],
    config: KitConfig,
    workspace: Path,
) -> list[str]:
    """Changed source files entirely absent from the coverage report.

    coverage.py without a ``source`` setting omits a file no test executed rather
    than listing it at 0%, so a brand-new untested module simply vanishes from
    coverage.xml — and _measure() would then see zero changed lines and wave the
    diff through. This recovers exactly those files: changed ``.py`` under the
    package root, carrying real code, that the report never mentions.
    """
    absent: list[str] = []
    for path in changed:
        if _coverage_for(files, path) is not None or not _is_source_path(path, config):
            continue
        file_path = workspace / path
        try:
            source = file_path.read_text(encoding="utf-8")
        except OSError:
            # Renamed-away / deleted source has no lines to cover.
            continue
        if _file_has_executable_code(source):
            absent.append(path)
    return sorted(absent)


def _git_diff(workspace: Path, base_ref: str) -> str:
    # `base...HEAD` diffs against the MERGE-BASE, so unrelated commits landing on
    # the base branch after this one forked are not attributed to this diff.
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "diff", "--unified=0", "--no-color", f"{base_ref}...HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    except FileNotFoundError as exc:  # pragma: no cover - git is present in CI
        raise CoverageError("git is not on PATH") from exc
    if result.returncode != 0:
        raise CoverageError(f"git diff against {base_ref!r} failed: {result.stderr.strip()}")
    return result.stdout


def evaluate(
    result: DiffCoverage,
    baseline: float | None,
    *,
    uncovered_source: tuple[str, ...] = (),
) -> GateOutcome:
    """Apply the three §6.5 obligations to a measurement. Pure.

    The three obligations are enforced INDEPENDENTLY, because each is gameable
    while satisfying the other two:
      1. diff line >= 80% — only meaningful when there are measured changed lines;
      2. diff branch >= 70% — likewise;
      3. project-wide never-decrease — does NOT depend on the diff at all, so it
         runs unconditionally. Gating it behind "the diff has measured lines"
         would let a test-deleting PR (whose diff touches only test files) drop
         project coverage and still pass.
    Plus the report-omission guard: a changed source file the report never
    mentions is untested new code, not a docs change.
    """
    findings: list[Finding] = []

    def add(message: str, fix: str, path: str = COVERAGE_XML) -> None:
        findings.append(Finding(wfl_id=WFL_ID, path=path, line=0, message=message, fix=fix))

    for path in uncovered_source:
        add(
            f"changed source file {path!r} has no coverage data — no test executed it",
            "add unit tests exercising this file (§6.5 TITAN-WFL-030)",
            path=path,
        )

    if result.lines_total and result.line_rate < MIN_LINE_COVERAGE:
        add(
            f"diff line coverage {result.line_rate:.1f}% is below the "
            f"{MIN_LINE_COVERAGE:.0f}% gate "
            f"({result.lines_covered}/{result.lines_total} changed lines covered)",
            "add unit tests for the changed lines (§6.5 TITAN-WFL-030)",
        )
    if result.branches_total and result.branch_rate < MIN_BRANCH_COVERAGE:
        add(
            f"diff branch coverage {result.branch_rate:.1f}% is below the "
            f"{MIN_BRANCH_COVERAGE:.0f}% gate "
            f"({result.branches_taken}/{result.branches_total} changed branches taken)",
            "cover both edges of the changed conditionals (§6.5 TITAN-WFL-030)",
        )
    if baseline is not None and result.project_rate < baseline:
        add(
            f"project-wide coverage fell from {baseline:.2f}% to {result.project_rate:.2f}%",
            "coverage may never decrease project-wide (§6.5 TITAN-WFL-030)",
        )

    if findings:
        return failed(GATE, WFL_ID, tuple(findings))
    return passed(GATE, WFL_ID)


def read_baseline(workspace: Path) -> float | None:
    """The last recorded project-wide rate, or None on the first ever run."""
    path = workspace / BASELINE_PATH
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data["project_rate"]
        if not isinstance(value, (int, float)):
            raise TypeError(type(value))
        return float(value)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        # A corrupt baseline must not silently disable the never-decrease rule.
        raise CoverageError(f"{BASELINE_PATH} is unreadable: {exc}") from exc


def write_baseline(workspace: Path, project_rate: float) -> None:
    path = workspace / BASELINE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"project_rate": round(project_rate, 4)}, indent=2) + "\n",
        encoding="utf-8",
    )


def check(workspace: Path, *, update_baseline: bool = False) -> GateOutcome:
    config = load_config(workspace)

    xml_path = workspace / COVERAGE_XML
    if not xml_path.is_file():
        raise CoverageError(
            f"no {COVERAGE_XML} in the workspace — run the test suite with "
            "`--cov-report=xml` before this gate"
        )

    diff = _git_diff(workspace, config.base_ref)
    changed = parse_diff(diff)
    files, project_rate = parse_coverage_xml(xml_path.read_text(encoding="utf-8"))
    result = _measure(changed, files, project_rate)
    uncovered = tuple(untested_source_files(changed, files, config, workspace))

    # evaluate() runs unconditionally — the never-decrease obligation is
    # independent of the diff, so there is no "nothing changed, skip everything"
    # fast path (that was the bug that let a test-deleting PR through).
    outcome = evaluate(result, read_baseline(workspace), uncovered_source=uncovered)

    # Only record a new floor when the gate passed. Writing the baseline on a
    # FAILING run would ratchet the floor DOWN to the just-rejected rate, so the
    # next PR could re-fail at the lower number and pass.
    if update_baseline and outcome.ok:
        write_baseline(workspace, result.project_rate)
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{WFL_ID}: §6.5 coverage gates on the diff")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help=f"workspace root (must contain {CONFIG_FILENAME} and {COVERAGE_XML})",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record the project-wide rate as the never-decrease floor (run on the base branch)",
    )
    args = parser.parse_args(argv)
    try:
        outcome = check(args.workspace, update_baseline=args.update_baseline)
    except (CoverageError, ConfigError) as exc:
        print(f"ERROR {GATE} ({WFL_ID}): {exc}")
        return 2
    print(outcome.render())
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
