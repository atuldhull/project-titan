"""``git.*`` builtin tools (provider ``builtin:git``) — version control on the
local repo in the task sandbox's /workspace (Vol 7 §4.1 & §5).

Every git invocation is argv (never a shell string) run inside the sandbox.
Writes are constrained to ``titan/task-*`` branches (TITAN-SEC-031): branch
creation and push validate the branch-name argument, while commit/merge/rebase
verify the sandbox's CURRENT branch before mutating it. ``git.commit`` matches
the §4.1 registry entry exactly (message minLength 10, paths[], author_agent →
commit_sha + files_changed). Remotes are reachable only via connector
credentials, so push/clone run against local fixtures for now.
"""

from typing import Any

from titan.tools.errors import ToolError, ToolErrorCode
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolSpec
from titan.tools.sandbox.controller import ExecResult, Sandbox
from titan.tools.sandbox.policy import ResourceLimits
from titan.tools.sandbox.session import SandboxSession

_TASK_BRANCH_PREFIX = "titan/task-"
# safe.directory='*' avoids git's "dubious ownership" refusal when the
# bind-mounted /workspace is owned by a different uid than the runtime user.
_GIT_BASE = ["git", "-c", "safe.directory=*"]


def _require_sandbox(ctx: ToolExecutionContext) -> SandboxSession:
    if ctx.sandbox is None:
        raise ToolError("TOOL_UNAVAILABLE", "no sandbox is available for this task")
    return ctx.sandbox


async def _git(sandbox: Sandbox, argv: list[str], *, timeout_s: float = 30.0) -> ExecResult:
    return await sandbox.exec(_GIT_BASE + argv, limits=ResourceLimits(timeout_s=timeout_s))


async def _current_branch(sandbox: Sandbox) -> str:
    # `branch --show-current` is defined on an UNBORN branch (returns the name),
    # unlike `rev-parse --abbrev-ref HEAD` which prints the literal 'HEAD' there
    # — so a legitimate first commit on a fresh titan/task-* branch isn't denied.
    # Detached HEAD returns "" here, which still fails the task-branch check.
    result = await _git(sandbox, ["branch", "--show-current"])
    return result.stdout.strip()


async def _require_task_branch(sandbox: Sandbox) -> str:
    """TITAN-SEC-031: commit/merge/rebase may only mutate a titan/task-* branch."""
    branch = await _current_branch(sandbox)
    if not branch.startswith(_TASK_BRANCH_PREFIX):
        raise ToolError(
            "PERMISSION_DENIED",
            f"git writes are only allowed on {_TASK_BRANCH_PREFIX}* branches (on {branch!r})",
        )
    return branch


def _task_branch_constraint(field: str) -> Any:
    """A TITAN-SEC-031 param constraint: the named branch argument must be a bare
    titan/task-* branch — never a refspec, force-push, or ref expression (used
    where the branch is an argument, not the current HEAD)."""

    def check(args: dict[str, Any]) -> "tuple[ToolErrorCode, str] | None":
        value = args.get(field)
        if value is None:
            return None
        text = str(value)
        # A refspec (src:dst), force (+), or ref-expression (^ ~ @{ whitespace)
        # could redirect the write off the task branch (e.g. titan/task-1:main).
        if any(ch in text for ch in ":+^~ \t\n") or "@{" in text:
            return (
                "PERMISSION_DENIED",
                f"at '/{field}': must be a bare {_TASK_BRANCH_PREFIX}* branch, not a refspec",
            )
        if not text.startswith(_TASK_BRANCH_PREFIX):
            return ("PERMISSION_DENIED", f"at '/{field}': must be a {_TASK_BRANCH_PREFIX}* branch")
        return None

    return check


def _remote_constraint(args: dict[str, Any]) -> "tuple[ToolErrorCode, str] | None":
    """A push remote must be a configured NAME, not a URL/local path (which would
    let an agent push outside the connector-scoped remotes)."""
    remote = str(args.get("remote", "origin"))
    if remote and (any(ch in remote for ch in ":/@\\") or remote.startswith(".")):
        return ("PERMISSION_DENIED", "at '/remote': must be a configured remote name, not a URL")
    return None


# --- read tools --------------------------------------------------------------


async def _status(_args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    sandbox = await _require_sandbox(ctx).get()
    result = await _git(sandbox, ["status", "--porcelain=v1", "--branch"])
    if not result.ok and "not a git repository" in result.stderr.lower():
        raise ToolError("UPSTREAM_ERROR", "the workspace is not a git repository")
    files, branch = [], ""
    for line in result.stdout.splitlines():
        if line.startswith("##"):
            branch = line[2:].strip().split("...")[0]
        elif line:
            files.append({"status": line[:2].strip(), "path": line[3:]})
    return ExecutionOutcome(result={"branch": branch, "clean": not files, "files": files})


async def _diff(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    sandbox = await _require_sandbox(ctx).get()
    argv = ["diff"]
    if args.get("staged"):
        argv.append("--cached")
    if args.get("paths"):
        argv += ["--", *[str(p) for p in args["paths"]]]
    result = await _git(sandbox, argv)
    return ExecutionOutcome(result={"diff": result.stdout, "exit_code": result.exit_code})


async def _log(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    sandbox = await _require_sandbox(ctx).get()
    max_count = min(int(args.get("max_count", 20)), 200)
    result = await _git(sandbox, ["log", f"-n{max_count}", "--pretty=format:%H%x1f%s"])
    commits = []
    for line in result.stdout.splitlines():
        if "\x1f" in line:
            sha, subject = line.split("\x1f", 1)
            commits.append({"sha": sha, "subject": subject})
    return ExecutionOutcome(result={"commits": commits})


async def _branch(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    sandbox = await _require_sandbox(ctx).get()
    name = args.get("name")
    if name is None:  # list
        result = await _git(sandbox, ["branch", "--format=%(refname:short)"])
        branches = [b.strip() for b in result.stdout.splitlines() if b.strip()]
        current = await _current_branch(sandbox)
        return ExecutionOutcome(result={"branches": branches, "current": current})
    # create + switch (branch name already constrained to titan/task-* by the spec)
    result = await _git(sandbox, ["checkout", "-b", str(name)])
    if not result.ok:
        raise ToolError("UPSTREAM_ERROR", f"git branch failed: {result.stderr.strip()}")
    return ExecutionOutcome(result={"created": True, "branch": str(name)})


# --- write tools (SEC-031 task-branch enforced) ------------------------------


async def _commit(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    sandbox = await _require_sandbox(ctx).get()
    await _require_task_branch(sandbox)
    paths = [str(p) for p in args["paths"]]
    add = await _git(sandbox, ["add", "--", *paths])
    if not add.ok:
        raise ToolError("UPSTREAM_ERROR", f"git add failed: {add.stderr.strip()}")
    author = str(args.get("author_agent", "titan-agent"))
    commit = await _git(
        sandbox,
        [
            "-c",
            f"user.name={author}",
            "-c",
            "user.email=agent@titan.local",
            "commit",
            "-m",
            str(args["message"]),
        ],
    )
    if not commit.ok:
        raise ToolError(
            "UPSTREAM_ERROR", f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"
        )
    sha = (await _git(sandbox, ["rev-parse", "HEAD"])).stdout.strip()
    # --root so a ROOT (parentless first) commit still lists its files rather
    # than reporting zero changes.
    changed = await _git(
        sandbox, ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "HEAD"]
    )
    files_changed = len([ln for ln in changed.stdout.splitlines() if ln.strip()])
    return ExecutionOutcome(result={"commit_sha": sha, "files_changed": files_changed})


async def _merge(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    sandbox = await _require_sandbox(ctx).get()
    await _require_task_branch(sandbox)
    result = await _git(
        sandbox,
        [
            "-c",
            "user.name=titan-agent",
            "-c",
            "user.email=agent@titan.local",
            "merge",
            "--no-edit",
            str(args["branch"]),
        ],
    )
    return ExecutionOutcome(
        result={
            "merged": result.ok,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )


async def _rebase(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    sandbox = await _require_sandbox(ctx).get()
    await _require_task_branch(sandbox)
    result = await _git(sandbox, ["rebase", str(args["onto"])])
    return ExecutionOutcome(
        result={"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}
    )


async def _push(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    sandbox = await _require_sandbox(ctx).get()
    remote = str(args.get("remote", "origin"))
    branch = str(args["branch"])  # constrained to a bare titan/task-* name
    # Pin BOTH sides of the refspec server-side so the destination can never be
    # anything but the same task ref, even if the constraint were bypassed.
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    result = await _git(sandbox, ["push", remote, refspec], timeout_s=60.0)
    return ExecutionOutcome(
        result={"exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}
    )


async def _clone(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_sandbox(ctx)
    sandbox = await session.get()
    dest = str(args.get("dir", "")).strip()
    if dest:
        session.workspace.resolve(dest)  # jail the destination
    result = await _git(
        sandbox, ["clone", str(args["url"]), *([dest] if dest else [])], timeout_s=120.0
    )
    if not result.ok:
        raise ToolError("UPSTREAM_ERROR", f"git clone failed: {result.stderr.strip()}")
    return ExecutionOutcome(result={"dir": dest or ".", "stdout": result.stdout})


# --- specs -------------------------------------------------------------------

_PATHS = {"type": "array", "items": {"type": "string"}, "maxItems": 500}
_OBJ = {"type": "object"}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _spec(
    name: str,
    description: str,
    risk: str,
    scope: str,
    executor: Any,
    input_schema: dict[str, Any],
    output_schema: dict[str, Any],
    *,
    constraints: tuple[Any, ...] = (),
) -> ToolSpec:
    return ToolSpec(
        name=name,
        provider="builtin:git",
        description=description,
        risk=risk,
        required_scopes=(scope,),
        input_schema=input_schema,
        output_schema=output_schema,
        executor=executor,
        timeout_s=180.0,
        rate_limit_per_min=120,
        constraints=constraints,
    )


# The §4.1 git.commit registry entry, verbatim.
_COMMIT_INPUT = _obj(
    {
        "message": {"type": "string", "minLength": 10},
        "paths": _PATHS,
        "author_agent": {"type": "string"},
    },
    ["message", "paths"],
)
_COMMIT_OUTPUT = {
    "type": "object",
    "properties": {"commit_sha": {"type": "string"}, "files_changed": {"type": "integer"}},
    "required": ["commit_sha"],
}

SPECS = (
    _spec(
        "git.status",
        "Show working-tree status.",
        "read",
        "workspace:read",
        _status,
        _obj({}, []),
        _OBJ,
    ),
    _spec(
        "git.diff",
        "Show the working-tree or staged diff.",
        "read",
        "workspace:read",
        _diff,
        _obj({"paths": _PATHS, "staged": {"type": "boolean"}}, []),
        _OBJ,
    ),
    _spec(
        "git.log",
        "List recent commits.",
        "read",
        "workspace:read",
        _log,
        _obj({"max_count": {"type": "integer", "minimum": 1, "maximum": 200}}, []),
        _OBJ,
    ),
    _spec(
        "git.branch",
        "List branches, or create+switch a titan/task-* branch.",
        "write",
        "workspace:write",
        _branch,
        _obj({"name": {"type": "string", "minLength": 1, "maxLength": 255}}, []),
        _OBJ,
        constraints=(_task_branch_constraint("name"),),
    ),
    _spec(
        "git.commit",
        "Create a commit in the workspace repository.",
        "write",
        "workspace:write",
        _commit,
        _COMMIT_INPUT,
        _COMMIT_OUTPUT,
    ),
    _spec(
        "git.merge",
        "Merge a branch into the current titan/task-* branch.",
        "write",
        "workspace:write",
        _merge,
        _obj({"branch": {"type": "string", "minLength": 1}}, ["branch"]),
        _OBJ,
    ),
    _spec(
        "git.rebase",
        "Rebase the current titan/task-* branch onto a base.",
        "write",
        "workspace:write",
        _rebase,
        _obj({"onto": {"type": "string", "minLength": 1}}, ["onto"]),
        _OBJ,
    ),
    _spec(
        "git.push",
        "Push a titan/task-* branch to a remote (connector-scoped).",
        "write",
        "workspace:write",
        _push,
        _obj(
            {"remote": {"type": "string"}, "branch": {"type": "string", "minLength": 1}}, ["branch"]
        ),
        _OBJ,
        constraints=(_task_branch_constraint("branch"), _remote_constraint),
    ),
    _spec(
        "git.clone",
        "Clone a repository into the workspace.",
        "write",
        "workspace:write",
        _clone,
        _obj({"url": {"type": "string", "minLength": 1}, "dir": {"type": "string"}}, ["url"]),
        _OBJ,
    ),
)

__all__ = ["SPECS"]
