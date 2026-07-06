"""``fs.*`` builtin tools (provider ``builtin:fs``) — the workspace-jailed file
operations of the Vol 7 §5 core tool set.

Every path is resolved through the task's :class:`~titan.tools.sandbox.workspace.Workspace`,
which follows symlinks and re-confines to ``/workspace`` (TITAN-SEC-073) — a
string-level ``workspace_path`` constraint on each spec is the first line of
defense, the resolver the second (it catches symlinks planted inside the jail).
``fs.patch`` applies unified diffs ATOMICALLY: every target file's new content is
computed and validated in memory first, then all files are written (temp+rename)
or none are, so a malformed or non-applying diff changes nothing.
"""

import os
import re
from pathlib import Path
from typing import Any

from titan.tools.constraints import workspace_path
from titan.tools.errors import ToolError
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolSpec
from titan.tools.sandbox.policy import TASK_MAX_WORKSPACE_BYTES
from titan.tools.sandbox.workspace import Workspace

_MAX_READ_BYTES = 5 * 1024 * 1024
_MAX_FILE_BYTES = 10 * 1024 * 1024
_MAX_GREP_MATCHES = 1000
_MAX_LIST_ENTRIES = 5000


def _require_workspace(ctx: ToolExecutionContext) -> Workspace:
    if ctx.workspace is None:
        raise ToolError("TOOL_UNAVAILABLE", "no workspace is provisioned for this task")
    return ctx.workspace


def _workspace_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            fp = Path(dirpath) / name
            if fp.is_symlink():
                continue
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


# --- executors ---------------------------------------------------------------


async def _read(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    ws = _require_workspace(ctx)
    target = ws.resolve(args["path"], must_exist=True)
    if target.is_dir():
        raise ToolError("SCHEMA_INVALID", f"at '/path': {args['path']} is a directory")
    data = target.read_bytes()
    truncated = len(data) > _MAX_READ_BYTES
    text = data[:_MAX_READ_BYTES].decode("utf-8", "replace")
    return ExecutionOutcome(
        result={
            "path": ws.relative(target),
            "content": text,
            "bytes": len(data),
            "truncated": truncated,
        }
    )


async def _write(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    ws = _require_workspace(ctx)
    content: str = args["content"]
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_FILE_BYTES:
        raise ToolError("SCHEMA_INVALID", f"at '/content': exceeds {_MAX_FILE_BYTES} bytes")
    target = ws.resolve(args["path"])
    if target.is_dir():
        raise ToolError("SCHEMA_INVALID", f"at '/path': {args['path']} is a directory")
    existing = target.stat().st_size if target.exists() and not target.is_symlink() else 0
    if _workspace_size(ws.root) - existing + len(encoded) > TASK_MAX_WORKSPACE_BYTES:
        raise ToolError("SANDBOX_VIOLATION", "workspace disk budget exceeded")
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, encoded)
    return ExecutionOutcome(result={"path": ws.relative(target), "bytes": len(encoded)})


async def _list(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    ws = _require_workspace(ctx)
    target = ws.resolve(args.get("path", "."), must_exist=True)
    if not target.is_dir():
        raise ToolError("SCHEMA_INVALID", f"at '/path': {args.get('path', '.')} is not a directory")
    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda p: p.name):
        kind = "dir" if child.is_dir() else ("symlink" if child.is_symlink() else "file")
        size = child.stat().st_size if kind == "file" else None
        entries.append({"name": child.name, "type": kind, "size": size})
        if len(entries) >= _MAX_LIST_ENTRIES:
            break
    return ExecutionOutcome(result={"path": ws.relative(target), "entries": entries})


async def _glob(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    ws = _require_workspace(ctx)
    pattern: str = args["pattern"]
    if pattern.startswith("/") or ".." in pattern.split("/"):
        raise ToolError("SANDBOX_VIOLATION", "glob pattern may not be absolute or traverse upward")
    matches: list[str] = []
    for match in sorted(ws.root.glob(pattern)):
        try:
            resolved = ws.resolve(match.relative_to(ws.root).as_posix())
        except ToolError:
            continue  # a symlinked match pointing outside — skip it
        if resolved.exists():
            matches.append(ws.relative(resolved))
        if len(matches) >= _MAX_LIST_ENTRIES:
            break
    return ExecutionOutcome(result={"pattern": pattern, "matches": matches})


async def _grep(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    ws = _require_workspace(ctx)
    try:
        regex = re.compile(args["pattern"])
    except re.error as exc:
        raise ToolError("SCHEMA_INVALID", f"at '/pattern': invalid regex: {exc}") from exc
    root = ws.resolve(args.get("path", "."), must_exist=True)
    glob = args.get("glob", "**/*")
    files = [root] if root.is_file() else sorted(root.glob(glob))
    matches: list[dict[str, Any]] = []
    for fp in files:
        if not fp.is_file() or fp.is_symlink():
            continue
        try:
            ws.resolve(fp.relative_to(ws.root).as_posix(), must_exist=True)
        except ToolError:
            continue
        try:
            text = fp.read_text("utf-8", "replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append({"path": ws.relative(fp), "line": lineno, "text": line[:500]})
                if len(matches) >= _MAX_GREP_MATCHES:
                    return ExecutionOutcome(result={"matches": matches, "truncated": True})
    return ExecutionOutcome(result={"matches": matches, "truncated": False})


async def _patch(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    ws = _require_workspace(ctx)
    plan = _plan_patch(ws, args["patch"])  # computes + validates ALL files first
    # Enforce the SAME per-file cap + workspace budget fs.write does, over the
    # whole plan BEFORE any write (so all-or-nothing atomicity is preserved and
    # fs.patch can't be a disk-budget bypass — TITAN-SEC-042).
    delta = 0
    for target, new_bytes in plan:
        existing = target.stat().st_size if target.exists() and not target.is_symlink() else 0
        if new_bytes is None:
            delta -= existing
        else:
            if len(new_bytes) > _MAX_FILE_BYTES:
                raise ToolError(
                    "SCHEMA_INVALID",
                    f"at '/patch': {ws.relative(target)} exceeds {_MAX_FILE_BYTES} bytes",
                )
            delta += len(new_bytes) - existing
    if _workspace_size(ws.root) + delta > TASK_MAX_WORKSPACE_BYTES:
        raise ToolError("SANDBOX_VIOLATION", "workspace disk budget exceeded")
    for target, new_bytes in plan:
        if new_bytes is None:
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, new_bytes)
    return ExecutionOutcome(
        result={"files_changed": len(plan), "paths": [ws.relative(p) for p, _ in plan]}
    )


# --- unified-diff engine (atomic; no external `patch` dependency) ------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_prefix(path: str) -> str:
    if path in ("/dev/null",):
        return path
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _plan_patch(ws: Workspace, patch: str) -> list[tuple[Path, bytes | None]]:
    """Parse a unified diff and compute every target's new content, validating
    that all hunks apply. Returns (resolved_path, new_bytes|None-for-delete).
    Raises SCHEMA_INVALID (malformed) or UPSTREAM_ERROR (context mismatch)."""
    lines = patch.splitlines()
    i, plan = 0, []
    saw_file = False
    while i < len(lines):
        line = lines[i]
        if not line.startswith("--- "):
            i += 1
            continue
        saw_file = True
        if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
            raise ToolError("SCHEMA_INVALID", "malformed diff: '---' without '+++'")
        old_path = _strip_prefix(line[4:].split("\t")[0].strip())
        new_path = _strip_prefix(lines[i + 1][4:].split("\t")[0].strip())
        i += 2
        hunks, i = _collect_hunks(lines, i)
        if not hunks:
            raise ToolError("SCHEMA_INVALID", "malformed diff: file header without a hunk")
        deleting = new_path == "/dev/null"
        path_str = old_path if deleting else new_path
        target = ws.resolve(path_str)
        # The target must be a concrete file — never the workspace root or an
        # existing directory (a '+++ b/' empty header resolves to the root and
        # would otherwise let _atomic_write escape into the shared parent dir).
        if not path_str or target == ws.root or target.is_dir():
            raise ToolError(
                "SCHEMA_INVALID", f"malformed diff: target {path_str!r} is empty or a directory"
            )
        original = target.read_text("utf-8", "replace") if target.exists() else ""
        new_text = _apply_hunks(original, hunks, path_str)
        plan.append((target, None if deleting else new_text.encode("utf-8")))
    if not saw_file:
        raise ToolError("SCHEMA_INVALID", "malformed diff: no '--- / +++' file header found")
    return plan


# A hunk carries its declared line counts so the body is read by COUNT, not by
# guessing the boundary from a '--- ' prefix (a deleted line whose text starts
# with '-- ' is a legal body line, not a new file header).
_Hunk = tuple[int, int, int, list[str]]


def _collect_hunks(lines: list[str], i: int) -> tuple[list[_Hunk], int]:
    hunks: list[_Hunk] = []
    while i < len(lines) and lines[i].startswith("@@"):
        m = _HUNK_RE.match(lines[i])
        if not m:
            raise ToolError("SCHEMA_INVALID", f"malformed hunk header: {lines[i]!r}")
        old_start = int(m.group(1))
        old_count = int(m.group(2)) if m.group(2) is not None else 1
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        i += 1
        body: list[str] = []
        old_rem, new_rem = old_count, new_count
        # Read EXACTLY the declared number of old/new lines.
        while (old_rem > 0 or new_rem > 0) and i < len(lines):
            entry = lines[i]
            if entry.startswith("\\"):  # '\ No newline at end of file' — no budget
                body.append(entry)
                i += 1
                continue
            tag = entry[0] if entry else " "  # a bare blank line is context
            if tag == " ":
                old_rem -= 1
                new_rem -= 1
            elif tag == "-":
                old_rem -= 1
            elif tag == "+":
                new_rem -= 1
            else:
                raise ToolError("SCHEMA_INVALID", f"malformed hunk line: {entry!r}")
            body.append(entry)
            i += 1
        if old_rem != 0 or new_rem != 0:
            raise ToolError("SCHEMA_INVALID", "malformed diff: hunk truncated before its counts")
        # Consume a trailing no-newline marker that follows the counted body.
        if i < len(lines) and lines[i].startswith("\\"):
            body.append(lines[i])
            i += 1
        hunks.append((old_start, old_count, new_count, body))
    return hunks, i


def _split_lines(original: str) -> tuple[list[str], bool]:
    """Logical lines + whether the original ended with a newline. An empty file
    has no logical lines; a file ending in '\\n' drops the phantom last split."""
    if original == "":
        return [], True
    if original.endswith("\n"):
        return original[:-1].split("\n"), True
    return original.split("\n"), False


def _apply_hunks(original: str, hunks: list[_Hunk], path: str) -> str:
    src, orig_trailing = _split_lines(original)
    out: list[str] = []
    cursor = 0  # 0-based index into src
    new_no_newline = False  # the NEW side's last line lacks a terminator
    touched_eof = False
    for old_start, old_count, _new_count, body in hunks:
        # Zero-context insertion (@@ -L,0 @@) inserts AFTER old line L, i.e. at
        # 0-based index L; a normal hunk starts at the first affected line L-1.
        target = old_start if old_count == 0 else (old_start - 1 if old_start > 0 else 0)
        if target < cursor or target > len(src):
            raise ToolError("UPSTREAM_ERROR", f"hunk for {path} does not apply (out of range)")
        out.extend(src[cursor:target])
        cursor = target
        last_tag = " "
        for entry in body:
            if entry.startswith("\\"):  # no-newline marker for the previous line
                if last_tag in (" ", "+"):
                    new_no_newline = True
                continue
            tag, text = (entry[0], entry[1:]) if entry else (" ", "")
            last_tag = tag
            if tag in (" ", "-"):
                if cursor >= len(src) or src[cursor] != text:
                    raise ToolError(
                        "UPSTREAM_ERROR", f"hunk for {path} does not apply (context mismatch)"
                    )
                if tag == " ":
                    out.append(src[cursor])
                cursor += 1
            else:  # '+'
                out.append(text)
        touched_eof = cursor == len(src)
    tail = src[cursor:]
    out.extend(tail)
    # An untouched tail keeps the original terminator; a hunk that reached EOF
    # lets the NEW side decide it (default: newline-terminated).
    trailing = orig_trailing if tail or not touched_eof else not new_no_newline
    result = "\n".join(out)
    return result + "\n" if trailing and out else result


def _atomic_write(target: Path, data: bytes) -> None:
    tmp = target.with_name(f".{target.name}.titan-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)  # atomic same-filesystem rename
    except OSError:
        tmp.unlink(missing_ok=True)  # never leave an orphan temp file behind
        raise


# --- specs -------------------------------------------------------------------

_PATH_PROP = {"type": "string", "minLength": 1, "maxLength": 4096}


def _spec(
    name: str,
    description: str,
    risk: str,
    scope: str,
    executor: Any,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    *,
    path_fields: tuple[str, ...] = ("path",),
) -> ToolSpec:
    return ToolSpec(
        name=name,
        provider="builtin:fs",
        description=description,
        risk=risk,
        required_scopes=(scope,),
        input_schema=input_schema,
        output_schema=output_schema,
        executor=executor,
        timeout_s=15.0,
        rate_limit_per_min=600,
        constraints=(workspace_path(*path_fields),) if path_fields else (),
    )


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
    _spec(
        "fs.read",
        "Read a UTF-8 text file from the workspace.",
        "read",
        "workspace:read",
        _read,
        _obj({"path": _PATH_PROP}, ["path"]),
        _OBJ,
    ),
    _spec(
        "fs.write",
        "Create or overwrite a workspace file with the given text (atomic).",
        "write",
        "workspace:write",
        _write,
        _obj(
            {"path": _PATH_PROP, "content": {"type": "string", "maxLength": 10_000_000}},
            ["path", "content"],
        ),
        _OBJ,
    ),
    _spec(
        "fs.list",
        "List the entries of a workspace directory.",
        "read",
        "workspace:read",
        _list,
        _obj({"path": _PATH_PROP}, []),
        _OBJ,
    ),
    _spec(
        "fs.glob",
        "Match workspace paths against a glob pattern (e.g. 'src/**/*.py').",
        "read",
        "workspace:read",
        _glob,
        _obj({"pattern": {"type": "string", "minLength": 1, "maxLength": 1024}}, ["pattern"]),
        _OBJ,
        path_fields=(),  # 'pattern' is not a plain path; jailed inside the executor
    ),
    _spec(
        "fs.grep",
        "Search workspace files for a regular expression.",
        "read",
        "workspace:read",
        _grep,
        _obj(
            {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 1024},
                "path": _PATH_PROP,
                "glob": {"type": "string", "maxLength": 1024},
            },
            ["pattern"],
        ),
        _OBJ,
    ),
    _spec(
        "fs.patch",
        "Apply a unified diff to workspace files atomically (all-or-nothing).",
        "write",
        "workspace:write",
        _patch,
        _obj({"patch": {"type": "string", "minLength": 1, "maxLength": 10_000_000}}, ["patch"]),
        _OBJ,
        path_fields=(),  # paths come from the diff headers; jailed in the engine
    ),
)

__all__ = ["SPECS"]
