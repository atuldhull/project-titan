"""``terminal.*`` builtin tools (provider ``builtin:terminal``) — argv command
execution INSIDE the per-task sandbox (Vol 7 §5, TITAN-INT-031).

Commands are argv arrays only — never a shell string — so hostile "filenames"
are passed as literal arguments and never interpreted (TITAN-SEC-073). Output is
streamed incrementally as ``tool.output`` pub/sub events for the dashboard Logs
view AND captured, capped at 1 MiB per stream with an explicit truncation marker;
a runaway is killed at its timeout. ``run_background`` detaches a process and
returns a handle (its sandbox PID) that ``kill`` terminates.
"""

import uuid
from typing import Any

from titan.tools.errors import ToolError
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolSpec
from titan.tools.sandbox.policy import ResourceLimits
from titan.tools.sandbox.session import SandboxSession
from titan.tools.sandbox.workspace import MOUNT_POINT

_DEFAULT_TIMEOUT_S = 60
_MAX_TIMEOUT_S = 900


def _require_sandbox(ctx: ToolExecutionContext) -> SandboxSession:
    if ctx.sandbox is None:
        raise ToolError("TOOL_UNAVAILABLE", "no sandbox is available for this task")
    return ctx.sandbox


def _workdir(session: SandboxSession, cwd: str | None) -> str:
    """A sandbox working directory confined to /workspace (jail-checked on the
    host side, then mapped to the in-sandbox mount)."""
    if not cwd:
        return MOUNT_POINT
    resolved = session.workspace.resolve(cwd)  # raises SANDBOX_VIOLATION on escape
    rel = session.workspace.relative(resolved)
    return MOUNT_POINT if rel == "." else f"{MOUNT_POINT}/{rel}"


def _env(args: dict[str, Any]) -> dict[str, str]:
    return {str(k): str(v) for k, v in (args.get("env") or {}).items()}


async def _run(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_sandbox(ctx)
    sandbox = await session.get()
    timeout = min(int(args.get("timeout_s", _DEFAULT_TIMEOUT_S)), _MAX_TIMEOUT_S)
    result = await sandbox.exec_streamed(
        list(args["argv"]),
        session.stream.emit,
        limits=ResourceLimits(timeout_s=float(timeout)),
        workdir=_workdir(session, args.get("cwd")),
        env=_env(args),
    )
    return ExecutionOutcome(
        result={
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "oom_killed": result.oom_killed,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        },
        stderr_tail=result.stderr,
    )


async def _run_background(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_sandbox(ctx)
    sandbox = await session.get()
    handle = uuid.uuid4().hex[:12]
    pid = await sandbox.start_background(
        list(args["argv"]),
        handle=handle,
        workdir=_workdir(session, args.get("cwd")),
        env=_env(args),
    )
    # The PID is the durable handle `kill` targets; the log dir holds its output.
    return ExecutionOutcome(
        result={"handle": str(pid), "pid": pid, "log_dir": f".titan/bg/{handle}"}
    )


async def _kill(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_sandbox(ctx)
    sandbox = await session.get()
    try:
        pid = int(args["handle"])
    except (ValueError, TypeError) as exc:
        raise ToolError("SCHEMA_INVALID", "at '/handle': not a valid process handle") from exc
    signal = str(args.get("signal", "TERM")).upper().removeprefix("SIG")
    if signal not in {"TERM", "KILL", "INT", "HUP"}:
        raise ToolError("SCHEMA_INVALID", "at '/signal': unsupported signal")
    killed = await sandbox.signal(pid, signal)
    return ExecutionOutcome(result={"killed": killed})


# --- specs -------------------------------------------------------------------

_ARGV = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 256}
_ENV = {"type": "object", "additionalProperties": {"type": "string"}}
_CWD = {"type": "string", "maxLength": 4096}
_OBJ = {"type": "object"}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


SPECS = (
    ToolSpec(
        name="terminal.run",
        provider="builtin:terminal",
        description="Run an argv command in the task sandbox (streamed, output-capped).",
        risk="write",
        required_scopes=("terminal:run",),
        input_schema=_obj(
            {
                "argv": _ARGV,
                "env": _ENV,
                "cwd": _CWD,
                "timeout_s": {"type": "integer", "minimum": 1, "maximum": _MAX_TIMEOUT_S},
            },
            ["argv"],
        ),
        output_schema=_OBJ,
        executor=_run,
        timeout_s=float(_MAX_TIMEOUT_S + 30),  # the in-sandbox timeout is the real bound
        rate_limit_per_min=120,
    ),
    ToolSpec(
        name="terminal.run_background",
        provider="builtin:terminal",
        description="Launch a detached argv process in the sandbox; returns a kill handle.",
        risk="write",
        required_scopes=("terminal:run",),
        input_schema=_obj({"argv": _ARGV, "env": _ENV, "cwd": _CWD}, ["argv"]),
        output_schema=_OBJ,
        executor=_run_background,
        timeout_s=30.0,
        rate_limit_per_min=60,
    ),
    ToolSpec(
        name="terminal.kill",
        provider="builtin:terminal",
        description="Signal a background process started by terminal.run_background.",
        risk="write",
        required_scopes=("terminal:run",),
        input_schema=_obj(
            {"handle": {"type": "string", "minLength": 1}, "signal": {"type": "string"}},
            ["handle"],
        ),
        output_schema=_OBJ,
        executor=_kill,
        timeout_s=15.0,
        rate_limit_per_min=60,
    ),
)

__all__ = ["SPECS"]
