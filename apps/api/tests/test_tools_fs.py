"""fs.* tool + workspace-jail units (Phase 20; Vol 7 §5, TITAN-SEC-073).

No Docker: the fs tools are trusted tool-host code operating on a real host
workspace directory, so their jail, hostile-filename handling, and fs.patch
atomicity are provable without a sandbox container. The through-the-bus audited
SANDBOX_VIOLATION (DoD) is asserted with the DB (schema marker); the container
red-team lives in test_sandbox.py.
"""

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from titan.tools.builtin import fs as fs_tools
from titan.tools.errors import ToolError
from titan.tools.registry import ToolExecutionContext
from titan.tools.sandbox.workspace import Workspace, WorkspaceManager

from .conftest import run_async

_CAN_SYMLINK = os.name != "nt"


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    manager = WorkspaceManager(tmp_path / "root")
    return manager.for_call(uuid.uuid4(), uuid.uuid4())


def _ctx(workspace: Workspace) -> ToolExecutionContext:
    return ToolExecutionContext(
        # fs tools never touch the DB, so a null engine is fine in these units.
        engine=None,  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        agent_id="agent.backend.1",
        agent_uuid=None,
        workspace=workspace,
    )


def _run(coro_fn: Any, args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    return run_async(lambda: coro_fn(args, ctx))


# --- happy paths -------------------------------------------------------------


def test_write_read_roundtrip(workspace: Workspace) -> None:
    ctx = _ctx(workspace)
    written = _run(fs_tools._write, {"path": "src/app.py", "content": "a\nb\n"}, ctx)
    assert written.result == {"path": "src/app.py", "bytes": 4}
    read = _run(fs_tools._read, {"path": "src/app.py"}, ctx)
    assert read.result["content"] == "a\nb\n" and read.result["bytes"] == 4


def test_list_glob_grep(workspace: Workspace) -> None:
    ctx = _ctx(workspace)
    _run(fs_tools._write, {"path": "src/app.py", "content": "import os\nTOKEN=1\n"}, ctx)
    _run(fs_tools._write, {"path": "README.md", "content": "hi\n"}, ctx)
    listed = _run(fs_tools._list, {"path": "."}, ctx)
    assert {e["name"] for e in listed.result["entries"]} == {"src", "README.md"}
    globbed = _run(fs_tools._glob, {"pattern": "**/*.py"}, ctx)
    assert globbed.result["matches"] == ["src/app.py"]
    grepped = _run(fs_tools._grep, {"pattern": "TOKEN"}, ctx)
    assert grepped.result["matches"] == [{"path": "src/app.py", "line": 2, "text": "TOKEN=1"}]


def test_missing_file_read_is_tool_unavailable(workspace: Workspace) -> None:
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._read, {"path": "nope.txt"}, _ctx(workspace))
    assert exc.value.code == "TOOL_UNAVAILABLE"


# --- the jail (TITAN-SEC-073) ------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "src/../../../etc/passwd",
        "foo/../../bar",
        "a/b/../../../c",
    ],
)
def test_relative_escapes_are_jailed(workspace: Workspace, path: str) -> None:
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._read, {"path": path}, _ctx(workspace))
    assert exc.value.code == "SANDBOX_VIOLATION"


def test_nul_byte_path_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._read, {"path": "a\x00b"}, _ctx(workspace))
    assert exc.value.code == "SANDBOX_VIOLATION"


def test_absolute_host_path_is_treated_literally_inside_jail(workspace: Workspace) -> None:
    # '/etc/passwd' at the executor is workspace-relative 'etc/passwd' — a
    # literal name that doesn't exist, never the host file. (Through the bus,
    # the workspace_path constraint rejects absolutes first — see the bus test.)
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._read, {"path": "/etc/passwd"}, _ctx(workspace))
    assert exc.value.code == "TOOL_UNAVAILABLE"  # not found in the jail, not a host read
    # …and writing it lands INSIDE the workspace, as a literal 'etc/passwd'.
    _run(fs_tools._write, {"path": "/etc/passwd", "content": "x"}, _ctx(workspace))
    assert (workspace.root / "etc" / "passwd").read_text() == "x"


@pytest.mark.skipif(not _CAN_SYMLINK, reason="symlink creation needs privilege on Windows")
def test_symlink_escape_is_jailed(workspace: Workspace, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    (workspace.root / "link").symlink_to(secret)
    # A symlink planted inside the jail pointing outside is caught by the
    # realpath resolver (the string-level constraint can't see it).
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._read, {"path": "link"}, _ctx(workspace))
    assert exc.value.code == "SANDBOX_VIOLATION"


def test_hostile_filenames_are_literal_not_executed(workspace: Workspace) -> None:
    ctx = _ctx(workspace)
    # Filesystem-portable hostile names (Windows forbids | : * ? < > \t in
    # filenames; those metachar names are exercised on Linux in test_sandbox.py).
    for name in ["; rm -rf .txt", "$(whoami).txt", "a b c.txt", "&& evil.txt", "néé.txt"]:
        _run(fs_tools._write, {"path": name, "content": "ok"}, ctx)
        assert (workspace.root / name).read_text() == "ok"  # created literally
    listed = _run(fs_tools._list, {"path": "."}, ctx)
    assert "; rm -rf .txt" in {e["name"] for e in listed.result["entries"]}


def test_glob_pattern_cannot_traverse_up(workspace: Workspace) -> None:
    for pattern in ["../*", "/etc/*", "a/../../*"]:
        with pytest.raises(ToolError) as exc:
            _run(fs_tools._glob, {"pattern": pattern}, _ctx(workspace))
        assert exc.value.code == "SANDBOX_VIOLATION"


# --- fs.patch atomicity + malformed rejection --------------------------------


def test_patch_modifies_and_creates_and_deletes(workspace: Workspace) -> None:
    ctx = _ctx(workspace)
    _run(fs_tools._write, {"path": "app.py", "content": "line1\nline2\n"}, ctx)
    _run(fs_tools._write, {"path": "old.txt", "content": "gone\n"}, ctx)
    diff = (
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n line1\n-line2\n+LINE2\n"
        "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,1 @@\n+fresh\n"
        "--- a/old.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-gone\n"
    )
    result = _run(fs_tools._patch, {"patch": diff}, ctx)
    assert result.result["files_changed"] == 3
    assert (workspace.root / "app.py").read_text() == "line1\nLINE2\n"
    assert (workspace.root / "new.txt").read_text() == "fresh\n"
    assert not (workspace.root / "old.txt").exists()


def test_patch_is_atomic_on_context_mismatch(workspace: Workspace) -> None:
    ctx = _ctx(workspace)
    _run(fs_tools._write, {"path": "a.txt", "content": "aaa\n"}, ctx)
    _run(fs_tools._write, {"path": "b.txt", "content": "bbb\n"}, ctx)
    before = ((workspace.root / "a.txt").read_text(), (workspace.root / "b.txt").read_text())
    # a.txt applies; b.txt's context is wrong — the WHOLE patch must be rejected
    # and NEITHER file changed (all-or-nothing).
    bad = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-aaa\n+AAA\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1,1 +1,1 @@\n-WRONG\n+BBB\n"
    )
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._patch, {"patch": bad}, ctx)
    assert exc.value.code == "UPSTREAM_ERROR"
    assert (
        (workspace.root / "a.txt").read_text(),
        (workspace.root / "b.txt").read_text(),
    ) == before


@pytest.mark.parametrize(
    "patch",
    [
        "not a diff at all",
        "--- a/x.txt\n",  # '---' without '+++'
        "--- a/x.txt\n+++ b/x.txt\n",  # file header without a hunk
        "--- a/x.txt\n+++ b/x.txt\n@@ bad hunk @@\n+z\n",  # malformed hunk header
    ],
)
def test_malformed_diffs_are_rejected(workspace: Workspace, patch: str) -> None:
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._patch, {"patch": patch}, _ctx(workspace))
    assert exc.value.code == "SCHEMA_INVALID"


def test_patch_targeting_escape_is_jailed(workspace: Workspace) -> None:
    escape = "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -0,0 +1,1 @@\n+pwned\n"
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._patch, {"patch": escape}, _ctx(workspace))
    assert exc.value.code == "SANDBOX_VIOLATION"


def test_patch_empty_target_is_rejected_no_orphan(workspace: Workspace) -> None:
    # A '+++ b/' empty header resolves to the workspace ROOT; the atomic-write
    # temp would land in the shared PARENT dir (outside the jail). Reject it, and
    # prove no orphan '.titan-tmp' escaped into ws.root.parent.
    before = set(workspace.root.parent.iterdir())
    for header in [
        "--- /dev/null\n+++ b/\n@@ -0,0 +1 @@\n+pwned\n",
        "--- a/\n+++ b/\n@@ -0,0 +1 @@\n+x\n",
    ]:
        with pytest.raises(ToolError) as exc:
            _run(fs_tools._patch, {"patch": header}, _ctx(workspace))
        assert exc.value.code == "SANDBOX_VIOLATION" or exc.value.code == "SCHEMA_INVALID"
    assert set(workspace.root.parent.iterdir()) == before  # no escaped temp file


def test_patch_zero_context_insertion_placement(workspace: Workspace) -> None:
    # @@ -2,0 +3,1 @@ (git diff -U0) inserts AFTER old line 2 — matching git apply.
    ctx = _ctx(workspace)
    _run(fs_tools._write, {"path": "f", "content": "1\n2\n3\n"}, ctx)
    _run(fs_tools._patch, {"patch": "--- a/f\n+++ b/f\n@@ -2,0 +3,1 @@\n+X\n"}, ctx)
    assert _run(fs_tools._read, {"path": "f"}, ctx).result["content"] == "1\n2\nX\n3\n"


def test_patch_deletion_line_starting_with_dashes(workspace: Workspace) -> None:
    # A deleted line whose text is '-- old comment' must NOT be misread as a new
    # file header (the engine reads by declared hunk counts, not a '--- ' guess).
    ctx = _ctx(workspace)
    _run(fs_tools._write, {"path": "q.sql", "content": "SELECT 1;\n-- old comment\n"}, ctx)
    diff = "--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,1 @@\n SELECT 1;\n--- old comment\n"
    _run(fs_tools._patch, {"patch": diff}, ctx)
    assert _run(fs_tools._read, {"path": "q.sql"}, ctx).result["content"] == "SELECT 1;\n"


def test_patch_no_newline_semantics(workspace: Workspace) -> None:
    ctx = _ctx(workspace)
    nomark = "\\ No newline at end of file"
    # Create a file with NO trailing newline via the marker.
    _run(
        fs_tools._patch,
        {"patch": f"--- /dev/null\n+++ b/nonl\n@@ -0,0 +1 @@\n+hello\n{nomark}\n"},
        ctx,
    )
    assert _run(fs_tools._read, {"path": "nonl"}, ctx).result["content"] == "hello"
    # ADD a missing trailing newline to an existing no-newline file.
    _run(fs_tools._write, {"path": "addnl", "content": "hi"}, ctx)
    _run(
        fs_tools._patch,
        {"patch": f"--- a/addnl\n+++ b/addnl\n@@ -1 +1 @@\n-hi\n{nomark}\n+hi\n"},
        ctx,
    )
    assert _run(fs_tools._read, {"path": "addnl"}, ctx).result["content"] == "hi\n"


def test_patch_truncated_hunk_is_rejected(workspace: Workspace) -> None:
    ctx = _ctx(workspace)
    _run(fs_tools._write, {"path": "f", "content": "a\nb\nc\n"}, ctx)
    # Header claims 3 old lines but only 1 body line is supplied.
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._patch, {"patch": "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n"}, ctx)
    assert exc.value.code == "SCHEMA_INVALID"


def test_patch_respects_the_per_file_cap(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fs.patch must enforce the SAME per-file cap fs.write does (not be a bypass).
    monkeypatch.setattr(fs_tools, "_MAX_FILE_BYTES", 32)
    ctx = _ctx(workspace)
    big = "+" + "A" * 100 + "\n"
    diff = f"--- /dev/null\n+++ b/big.txt\n@@ -0,0 +1 @@\n{big}"
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._patch, {"patch": diff}, ctx)
    assert exc.value.code == "SCHEMA_INVALID"
    assert not (workspace.root / "big.txt").exists()  # atomic: nothing written


def test_patch_respects_the_workspace_budget(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs_tools, "TASK_MAX_WORKSPACE_BYTES", 64)
    ctx = _ctx(workspace)
    diff = "--- /dev/null\n+++ b/f.txt\n@@ -0,0 +1 @@\n+" + "A" * 200 + "\n"
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._patch, {"patch": diff}, ctx)
    assert exc.value.code == "SANDBOX_VIOLATION"
    assert not (workspace.root / "f.txt").exists()


def test_no_workspace_provisioned_fails_closed(workspace: Workspace) -> None:
    ctx = ToolExecutionContext(
        engine=None,  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        task_id=None,
        agent_id="a",
        agent_uuid=None,
        workspace=None,
    )
    with pytest.raises(ToolError) as exc:
        _run(fs_tools._read, {"path": "x"}, ctx)
    assert exc.value.code == "TOOL_UNAVAILABLE"


# --- WorkspaceManager per-task isolation (TITAN-SEC-040) ---------------------


def test_reset_gives_distinct_clean_workspaces(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "root")
    project = uuid.uuid4()
    task_a, task_b = uuid.uuid4(), uuid.uuid4()
    ws_a = manager.reset(project, task_a)
    (ws_a.root / "leftover.txt").write_text("from task A")
    ws_b = manager.reset(project, task_b)
    assert ws_a.root != ws_b.root  # distinct dirs per task
    assert not (ws_b.root / "leftover.txt").exists()  # clean
    # Re-running the SAME task wipes its prior tree.
    ws_a2 = manager.reset(project, task_a)
    assert not (ws_a2.root / "leftover.txt").exists()


# --- through-the-bus DoD: host-read attempt → audited SANDBOX_VIOLATION ------


@pytest.mark.schema
def test_host_read_through_bus_is_sandbox_violation_and_audited(
    migrated_engine: Any, tmp_path: Path
) -> None:
    from sqlalchemy import text

    from titan.tools import repository as audit_repo
    from titan.tools.bus import ToolBus, ToolCaller, deny_all_approvals
    from titan.tools.envelopes import new_call
    from titan.tools.service import build_registry

    async def clean() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(text("TRUNCATE tool_audit"))

    run_async(clean)

    manager = WorkspaceManager(tmp_path / "root")
    bus = ToolBus(
        build_registry(),
        migrated_engine,
        approval_gate=deny_all_approvals,
        workspace_provider=manager,
    )
    project, task = uuid.uuid4(), uuid.uuid4()
    caller = ToolCaller(
        agent_id="agent.backend.1",
        role="backend",
        project_id=project,
        granted_scopes=frozenset({"workspace:read", "workspace:write"}),
        task_id=task,
    )
    call = new_call(
        "fs.read",
        {"path": "/etc/passwd"},
        project_id=project,
        agent_id=caller.agent_id,
        task_id=task,
    )
    result = run_async(lambda: bus.invoke(call, caller))
    # The workspace_path constraint rejects the absolute host path pre-execution.
    assert result.status == "error" and result.code == "SANDBOX_VIOLATION"

    async def audited() -> Any:
        async with migrated_engine.connect() as conn:
            return await audit_repo.rows_for_call(conn, call.tool_call_id)

    rows = run_async(audited)
    assert [r.status for r in rows] == ["error"]
    assert rows[0].error_code == "SANDBOX_VIOLATION"


@pytest.mark.schema
def test_fs_write_read_through_bus_roundtrip(migrated_engine: Any, tmp_path: Path) -> None:
    from titan.tools.bus import ToolBus, ToolCaller, deny_all_approvals
    from titan.tools.envelopes import new_call
    from titan.tools.service import build_registry

    manager = WorkspaceManager(tmp_path / "root")
    bus = ToolBus(
        build_registry(),
        migrated_engine,
        approval_gate=deny_all_approvals,
        workspace_provider=manager,
    )
    project, task = uuid.uuid4(), uuid.uuid4()
    caller = ToolCaller(
        agent_id="agent.backend.1",
        role="backend",
        project_id=project,
        granted_scopes=frozenset({"workspace:read", "workspace:write"}),
        task_id=task,
    )

    def invoke(tool: str, args: dict[str, Any]) -> Any:
        call = new_call(tool, args, project_id=project, agent_id=caller.agent_id, task_id=task)
        return run_async(lambda: bus.invoke(call, caller))

    assert invoke("fs.write", {"path": "src/x.py", "content": "print(1)\n"}).status == "ok"
    read = invoke("fs.read", {"path": "src/x.py"})
    assert read.status == "ok" and read.result["content"] == "print(1)\n"
