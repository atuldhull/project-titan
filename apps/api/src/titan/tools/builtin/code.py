"""``code.*`` builtin tools (provider ``builtin:code``) — convenience wrappers
over sandbox execution with structured result parsing (Vol 7 §5).

``run_python`` / ``run_node`` run inline code or a workspace file; ``lint`` runs
a linter and reports pass/fail; ``test`` runs a test command and then parses the
JUnit XML report + a Cobertura coverage report (both read from the bind-mounted
workspace on the host) into a typed result. The parsers are pure functions so
they are unit-testable without a sandbox.
"""

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from titan.tools.errors import ToolError
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolSpec
from titan.tools.sandbox.policy import ResourceLimits
from titan.tools.sandbox.session import SandboxSession


def _require_sandbox(ctx: ToolExecutionContext) -> SandboxSession:
    if ctx.sandbox is None:
        raise ToolError("TOOL_UNAVAILABLE", "no sandbox is available for this task")
    return ctx.sandbox


# --- pure parsers (unit-testable, no sandbox) --------------------------------


def parse_junit(xml: str) -> dict[str, Any]:
    """Parse a JUnit XML report into totals + the first failures. Accepts both a
    ``<testsuites>`` root and a bare ``<testsuite>``."""
    try:
        root = ET.fromstring(xml)  # noqa: S314 — trusted, sandbox-produced report
    except ET.ParseError as exc:
        raise ToolError("UPSTREAM_ERROR", f"could not parse JUnit XML: {exc}") from exc
    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failures: list[dict[str, str]] = []
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, 0))
        for case in suite.findall("testcase"):
            for kind in ("failure", "error"):
                node = case.find(kind)
                if node is not None and len(failures) < 50:
                    failures.append(
                        {
                            "name": f"{case.get('classname', '')}::{case.get('name', '')}".strip(
                                ":"
                            ),
                            "type": kind,
                            "message": (node.get("message") or (node.text or ""))[:500],
                        }
                    )
    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    return {
        "total": totals["tests"],
        "passed": max(0, passed),
        "failed": totals["failures"],
        "errors": totals["errors"],
        "skipped": totals["skipped"],
        "failures": failures,
    }


def parse_coverage(xml: str) -> float | None:
    """Parse a Cobertura coverage report's overall line coverage as a percent."""
    try:
        root = ET.fromstring(xml)  # noqa: S314 — trusted, sandbox-produced report
    except ET.ParseError:
        return None
    line_rate = root.get("line-rate")
    if line_rate is not None:
        try:
            return round(float(line_rate) * 100, 2)
        except ValueError:
            return None
    return None


# --- executors ---------------------------------------------------------------


async def _stream_run(
    ctx: ToolExecutionContext, argv: list[str], *, timeout_s: float = 300.0
) -> tuple[SandboxSession, Any]:
    session = _require_sandbox(ctx)
    sandbox = await session.get()
    result = await sandbox.exec_streamed(
        argv, session.stream.emit, limits=ResourceLimits(timeout_s=timeout_s)
    )
    return session, result


def _result_payload(result: Any) -> dict[str, Any]:
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "passed": result.ok,
    }


async def _run_python(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    argv = _interpreter_argv("python3", args)
    _, result = await _stream_run(ctx, argv)
    return ExecutionOutcome(result=_result_payload(result), stderr_tail=result.stderr)


async def _run_node(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    argv = _interpreter_argv("node", args, inline_flag="-e")
    _, result = await _stream_run(ctx, argv)
    return ExecutionOutcome(result=_result_payload(result), stderr_tail=result.stderr)


def _interpreter_argv(binary: str, args: dict[str, Any], *, inline_flag: str = "-c") -> list[str]:
    code, file = args.get("code"), args.get("file")
    if (code is None) == (file is None):
        raise ToolError("SCHEMA_INVALID", "provide exactly one of 'code' or 'file'")
    extra = [str(a) for a in (args.get("args") or [])]
    if code is not None:
        return [binary, inline_flag, str(code), *extra]
    return [binary, str(file), *extra]


async def _lint(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    linter = str(args["linter"])
    paths = [str(p) for p in (args.get("paths") or ["."])]
    argv = {
        "ruff": ["ruff", "check", *paths],
        "eslint": ["npx", "--no-install", "eslint", *paths],
        "prettier": ["npx", "--no-install", "prettier", "--check", *paths],
    }[linter]
    _, result = await _stream_run(ctx, argv, timeout_s=120.0)
    return ExecutionOutcome(result=_result_payload(result), stderr_tail=result.stderr)


async def _test(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    command = [str(c) for c in (args.get("command") or ["pytest", "-q"])]
    session, result = await _stream_run(ctx, command, timeout_s=600.0)
    payload: dict[str, Any] = {
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "stdout_tail": result.stdout[-4000:],
    }
    # The reports are written into the bind-mounted workspace — read them from
    # the host side directly (no second sandbox round-trip).
    junit = _read_workspace_file(session, str(args.get("junit_path", "junit.xml")))
    if junit is not None:
        payload["junit"] = parse_junit(junit)
    coverage = _read_workspace_file(session, str(args.get("coverage_path", "coverage.xml")))
    if coverage is not None:
        payload["coverage_percent"] = parse_coverage(coverage)
    return ExecutionOutcome(result=payload, stderr_tail=result.stderr)


def _read_workspace_file(session: SandboxSession, rel: str) -> str | None:
    try:
        target: Path = session.workspace.resolve(rel, must_exist=True)
    except ToolError:
        return None
    if target.is_dir():
        return None
    return target.read_text("utf-8", "replace")


# --- specs -------------------------------------------------------------------

_OBJ = {"type": "object"}
_ARGS = {"type": "array", "items": {"type": "string"}, "maxItems": 128}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _spec(name: str, description: str, executor: Any, schema: dict[str, Any]) -> ToolSpec:
    return ToolSpec(
        name=name,
        provider="builtin:code",
        description=description,
        risk="write",
        required_scopes=("terminal:run",),
        input_schema=schema,
        output_schema=_OBJ,
        executor=executor,
        timeout_s=630.0,
        rate_limit_per_min=120,
    )


_INTERP_SCHEMA = _obj(
    {
        "code": {"type": "string", "maxLength": 1_000_000},
        "file": {"type": "string", "maxLength": 4096},
        "args": _ARGS,
    },
    [],
)

SPECS = (
    _spec(
        "code.run_python", "Run inline Python or a workspace .py file.", _run_python, _INTERP_SCHEMA
    ),
    _spec(
        "code.run_node", "Run inline JavaScript or a workspace .js file.", _run_node, _INTERP_SCHEMA
    ),
    _spec(
        "code.lint",
        "Run a linter (ruff/eslint/prettier) and report pass/fail.",
        _lint,
        _obj(
            {
                "linter": {"type": "string", "enum": ["ruff", "eslint", "prettier"]},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 500},
            },
            ["linter"],
        ),
    ),
    _spec(
        "code.test",
        "Run a test command and parse its JUnit + coverage reports.",
        _test,
        _obj(
            {
                "command": _ARGS,
                "junit_path": {"type": "string", "maxLength": 4096},
                "coverage_path": {"type": "string", "maxLength": 4096},
            },
            [],
        ),
    ),
)

__all__ = ["SPECS", "parse_coverage", "parse_junit"]
