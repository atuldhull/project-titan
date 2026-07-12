"""Deterministic detectors for the §6.6 rejection rules R1-R11.

Each detector maps a :class:`PullRequest` to the findings for ONE rule, citing
file/line/rule/fix (TITAN-WFL-033). They are heuristics, not proofs — they fire
reliably on the canonical violation of their rule (the shape the regression
fixtures use) while staying quiet on clean code, because a reviewer that
false-rejects clean PRs is one the team learns to override. The reviewer AGENT
(LLM) still adds judgement on top; these give the loop a deterministic floor and
make the rubric regression-testable without a provider.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable

from titan.review.models import (
    ChangedFile,
    PullRequest,
    ReviewFinding,
    is_migration_path,
    is_test_path,
)

Detector = Callable[[PullRequest], list[ReviewFinding]]


def _finding(rule_code: str, file: str, line: int, message: str, suggestion: str) -> ReviewFinding:
    return ReviewFinding(
        rule_code=rule_code, file=file, line=line, message=message, suggestion=suggestion
    )


def _parse(file: ChangedFile) -> ast.Module | None:
    try:
        return ast.parse(file.content)
    except SyntaxError:
        return None


def _layer_of(path: str) -> str | None:
    for layer in ("domain", "application", "infrastructure", "interface"):
        if f"/{layer}/" in f"/{path}/":
            return layer
    return None


def _top_package(path: str) -> str | None:
    """The importing file's root package: the segment after ``src/`` (or the
    first segment). Used to keep R1 first-party — a third-party import like
    ``zope.interface`` shares a layer NAME but not the project package, and must
    not be flagged as a layering violation."""
    parts = [p for p in path.split("/") if p]
    if "src" in parts:
        parts = parts[parts.index("src") + 1 :]
    return parts[0] if parts else None


# --- R1: Clean Architecture / SOLID ------------------------------------------

_INWARD = {"domain": 0, "application": 1, "infrastructure": 2, "interface": 3}


def detect_r1(pr: PullRequest) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for file in pr.source_files():
        layer = _layer_of(file.path)
        pkg = _top_package(file.path)
        if layer is None or pkg is None:
            continue
        tree = _parse(file)
        if tree is None:
            continue
        for node in ast.walk(tree):
            module: str | None = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name if node.names else None
            else:
                continue
            if not module:
                continue
            # Only first-party imports (same root package) can violate this
            # project's layering — `zope.interface` is not a layer breach.
            if module.split(".", 1)[0] != pkg:
                continue
            for other, rank in _INWARD.items():
                # A layer importing a layer OUTSIDE it (higher rank) points outward.
                if f".{other}." in f".{module}." and rank > _INWARD[layer]:
                    findings.append(
                        _finding(
                            "R1",
                            file.path,
                            node.lineno,
                            f"{layer} layer imports {other} ({module!r}) — dependencies must "
                            "point inward only (§6.1)",
                            f"invert the dependency behind a port declared in {layer}",
                        )
                    )
    return findings


# --- R2: missing tests -------------------------------------------------------


def detect_r2(pr: PullRequest) -> list[ReviewFinding]:
    source = pr.source_files()
    if not source:
        return []
    if any(is_test_path(f.path) for f in pr.files):
        return []
    # A PR-level finding (line 0 = whole-file, always in the blast radius).
    return [
        _finding(
            "R2",
            source[0].path,
            0,
            "changed source ships with no test in the same PR (§6.5)",
            "add unit tests for the changed code in the same PR",
        )
    ]


# --- R3: hardcoded secret / missing input validation -------------------------

_SECRET_ASSIGN = re.compile(
    r"""(?ix)
    \b(?:password|passwd|secret|api[_-]?key|token|access[_-]?key|private[_-]?key|aws[_-]?secret)
    \s*[:=]\s*
    ['"]([^'"\s]{8,})['"]
    """
)
# Obvious secret-shaped literals regardless of the variable name (allow the
# `sk_live_` / `sk-live-` separators real provider keys use).
_SECRET_LITERAL = re.compile(
    r"(sk[_-][A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)"
)
# Placeholders / env indirection that are NOT secrets.
_NOT_SECRET = re.compile(r"(?i)(your[_-]?|example|placeholder|changeme|xxx+|os\.environ|getenv)")


def detect_r3(pr: PullRequest) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for file in pr.source_files():
        for index, raw in enumerate(file.content.splitlines(), start=1):
            hit = _SECRET_ASSIGN.search(raw) or _SECRET_LITERAL.search(raw)
            if hit and not _NOT_SECRET.search(raw):
                findings.append(
                    _finding(
                        "R3",
                        file.path,
                        index,
                        "hardcoded secret in source",
                        "load the secret from the environment / a secret manager, never inline",
                    )
                )
    return findings


# --- R4: duplicated logic (>10 similar lines) --------------------------------

_TRIVIAL = re.compile(r"^\s*(?:#.*|[)\]}]*:?\s*|pass|\.\.\.|return|else:|try:)?\s*$")


def _significant_lines(content: str) -> list[tuple[int, str]]:
    out = []
    for index, raw in enumerate(content.splitlines(), start=1):
        stripped = raw.strip()
        if stripped and not _TRIVIAL.match(raw) and not stripped.startswith("#"):
            out.append((index, stripped))
    return out


def detect_r4(pr: PullRequest, *, run_length: int = 10) -> list[ReviewFinding]:
    """A run of >= ``run_length`` significant lines duplicated verbatim elsewhere
    in the PR (within a file or across files)."""
    findings: list[ReviewFinding] = []
    files = pr.source_files()
    for file in files:
        lines = _significant_lines(file.content)
        seqs: dict[tuple[str, ...], int] = {}
        for start in range(len(lines) - run_length + 1):
            window = tuple(text for _, text in lines[start : start + run_length])
            first_line = lines[start][0]
            if window in seqs:
                findings.append(
                    _finding(
                        "R4",
                        file.path,
                        first_line,
                        f">{run_length - 1} duplicated lines (first seen at line {seqs[window]})",
                        "extract the repeated block into a shared function/abstraction",
                    )
                )
                break
            seqs[window] = first_line
    return findings


# --- R5: swallowed errors / bare excepts -------------------------------------


def detect_r5(pr: PullRequest) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for file in pr.source_files():
        tree = _parse(file)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = node.body
            is_swallowed = (
                len(body) == 1
                and isinstance(body[0], (ast.Pass, ast.Expr))
                and (
                    isinstance(body[0], ast.Pass)
                    or (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant))
                )
            )
            if node.type is None:
                findings.append(
                    _finding(
                        "R5",
                        file.path,
                        node.lineno,
                        "bare `except:` catches everything, including system exits (§6.3)",
                        "catch a specific exception; log-and-rethrow or handle, never swallow",
                    )
                )
            elif is_swallowed:
                findings.append(
                    _finding(
                        "R5",
                        file.path,
                        node.lineno,
                        "exception swallowed (caught then dropped) (§6.3)",
                        "log-and-rethrow or handle the error; do not pass",
                    )
                )
    return findings


# --- R6: CQRS/event-sourcing without a linked ADR ----------------------------


def detect_r6(pr: PullRequest) -> list[ReviewFinding]:
    cqrs = [f for f in pr.source_files() if "/commands/" in f.path or "/queries/" in f.path]
    if not cqrs:
        return []
    justified = any(
        "cqrs" in path.lower() or "event-sourc" in path.lower() for path in pr.adr_paths
    )
    if justified:
        return []
    return [
        _finding(
            "R6",
            cqrs[0].path,
            0,
            "CQRS split (commands//queries/) with no linked ADR justifying it (§6.1)",
            "add an ADR demonstrating the read/write asymmetry, or use standard use-cases",
        )
    ]


# --- R7: placeholder code ----------------------------------------------------

_TODO = re.compile(r"(?i)(?:#|//)\s*(?:todo|fixme|xxx|hack)\b")
# Commented-OUT CODE, distinguished from ordinary prose by requiring code
# STRUCTURE, not merely a leading keyword. `# if the value is negative` and
# `# import order matters` are prose and must NOT match; `# def f(`, `# x = 1`,
# `# if cond:` (ends in a colon), and `# import os` (nothing but the import) do.
_COMMENTED_CODE = re.compile(
    r"^\s*#\s*(?:"
    r"def\s+\w+\s*\(|"  # def f(
    r"class\s+\w+\s*[(:]|"  # class C: / class C(
    r"(?:from\s+[\w.]+\s+)?import\s+[\w.*]+(?:\s+as\s+\w+)?\s*$|"  # import x / from y import z
    r"[\w.]+\s*=\s*\S+|"  # x = something (an assignment, not prose)
    r"(?:if|elif|for|while|with|try|except|finally)\b[^#]*:\s*$"  # compound stmt ending in :
    r")"
)


def detect_r7(pr: PullRequest) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for file in pr.source_files():
        for index, raw in enumerate(file.content.splitlines(), start=1):
            if _TODO.search(raw):
                findings.append(
                    _finding(
                        "R7",
                        file.path,
                        index,
                        "TODO/FIXME placeholder left in code",
                        "resolve the placeholder or file a tracked ticket and remove the marker",
                    )
                )
            elif _COMMENTED_CODE.match(raw):
                findings.append(
                    _finding(
                        "R7",
                        file.path,
                        index,
                        "commented-out code",
                        "delete commented-out code (version control preserves history)",
                    )
                )
        tree = _parse(file)
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and _is_not_implemented(node):
                    findings.append(
                        _finding(
                            "R7",
                            file.path,
                            node.lineno,
                            "stubbed body raising NotImplementedError",
                            "implement the function or remove the stub before review",
                        )
                    )
    return findings


def _is_not_implemented(node: ast.Raise) -> bool:
    exc = node.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"


# --- R8: public API change without OpenAPI update ----------------------------

_ROUTE = re.compile(r"@\w+\.(?:get|post|put|patch|delete)\(|APIRouter\(|@(?:app|router)\.route\(")


def detect_r8(pr: PullRequest) -> list[ReviewFinding]:
    if pr.openapi_changed:
        return []
    findings: list[ReviewFinding] = []
    for file in pr.source_files():
        if "/interface/" not in file.path and "router" not in file.path:
            continue
        match = _ROUTE.search(file.content)
        if match:
            line = file.content[: match.start()].count("\n") + 1
            findings.append(
                _finding(
                    "R8",
                    file.path,
                    line,
                    f"public API route (line {line}) changed without an OpenAPI update in "
                    "the same PR",
                    "regenerate the OpenAPI spec and version the endpoint per the "
                    "ArchitecturePackage",
                )
            )
            break
    return findings


# --- R9: N+1 query introduced ------------------------------------------------

# Unambiguously DB-query methods. `get`/`first`/`all` are deliberately EXCLUDED:
# dict.get, cache.get, and collection .first()/.all() are common in-memory calls
# that a receiver merely named `db`/`session`/`repo` would false-positive on.
_QUERY_ATTR = frozenset(
    {
        "execute",
        "scalar",
        "scalars",
        "fetchone",
        "fetchall",
        "fetchmany",
        "fetch",
        "query",
    }
)


def detect_r9(pr: PullRequest) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for file in pr.source_files():
        tree = _parse(file)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                continue
            for inner in ast.walk(node):
                call = inner
                if isinstance(inner, ast.Await):
                    call = inner.value
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in _QUERY_ATTR
                    and _receiver_looks_like_db(call.func.value)
                ):
                    findings.append(
                        _finding(
                            "R9",
                            file.path,
                            call.lineno,
                            f"database call {call.func.attr!r} inside a loop — likely an N+1",
                            "batch the query outside the loop (a single IN/JOIN or a bulk fetch)",
                        )
                    )
                    break
    return findings


def _receiver_looks_like_db(node: ast.expr) -> bool:
    names = {"conn", "connection", "session", "db", "cursor", "repo", "repository"}
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return node.attr in names
    return False


# --- R10: naming / lint that bypassed CI -------------------------------------

_SNAKE = re.compile(r"^_?[a-z][a-z0-9_]*$")
_PASCAL = re.compile(r"^_?[A-Z][a-zA-Z0-9]*$")


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def detect_r10(pr: PullRequest) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for file in pr.source_files():
        tree = _parse(file)
        if tree is None:
            continue
        for node in ast.walk(tree):
            # Dunder methods (__init__, __repr__, __eq__, …) are the language's
            # own naming, not a §6.2 violation — never flag them.
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not _is_dunder(node.name)
                and not _SNAKE.match(node.name)
            ):
                findings.append(
                    _finding(
                        "R10",
                        file.path,
                        node.lineno,
                        f"function {node.name!r} is not snake_case (§6.2)",
                        "rename to snake_case",
                    )
                )
            elif isinstance(node, ast.ClassDef) and not _PASCAL.match(node.name):
                findings.append(
                    _finding(
                        "R10",
                        file.path,
                        node.lineno,
                        f"class {node.name!r} is not PascalCase (§6.2)",
                        "rename to PascalCase",
                    )
                )
    return findings


# --- R11: missing docstrings / migration rollback ----------------------------


def detect_r11(pr: PullRequest) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for file in pr.files:
        if is_migration_path(file.path):
            tree = _parse(file)
            if tree is not None and not _has_function(tree, "downgrade"):
                findings.append(
                    _finding(
                        "R11",
                        file.path,
                        1,
                        "migration has no downgrade() — a schema change must be reversible",
                        "add a downgrade() that reverses the upgrade()",
                    )
                )
            continue
    for file in pr.source_files():
        tree = _parse(file)
        if tree is None:
            continue
        for node in _public_defs(tree):
            if ast.get_docstring(node) is None:
                findings.append(
                    _finding(
                        "R11",
                        file.path,
                        node.lineno,
                        f"public {_kind(node)} {node.name!r} has no docstring (§6.7)",
                        "add a Google-style docstring explaining the contract",
                    )
                )
    return findings


def _has_function(tree: ast.Module, name: str) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in tree.body
    )


def _public_defs(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    out: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not (
            node.name.startswith("_")
        ):
            out.append(node)
    return out


def _kind(node: ast.AST) -> str:
    return "class" if isinstance(node, ast.ClassDef) else "function"


# The rubric of detectors, in R-order.
DETECTORS: dict[str, Detector] = {
    "R1": detect_r1,
    "R2": detect_r2,
    "R3": detect_r3,
    "R4": detect_r4,
    "R5": detect_r5,
    "R6": detect_r6,
    "R7": detect_r7,
    "R8": detect_r8,
    "R9": detect_r9,
    "R10": detect_r10,
    "R11": detect_r11,
}


__all__ = ["DETECTORS", "Detector"]
