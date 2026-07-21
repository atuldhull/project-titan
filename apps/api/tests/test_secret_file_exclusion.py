"""TITAN-SEC-055: secret-bearing file patterns are excluded from context
assembly BY DEFAULT at every file-reading surface — the fs.* builtins (the
only host-side path from repo files into agent context), the ``code.test``
report reader, and the plugin ``fs.read`` capability. No Docker, no DB: these
surfaces are trusted tool-host code over a real host workspace.

The DoD scenario (a repo containing ``.env`` and ``id_rsa``) drives every
tool: the files must be unreadable, unlistable, unglobbable, and ungreppable,
while sibling ordinary files stay fully available.
"""

import uuid
from pathlib import Path
from typing import Any

import pytest

from titan.plugins import jsonrpc
from titan.plugins.capabilities import CapabilityBroker
from titan.plugins.jsonrpc import RpcError
from titan.plugins.manifest import Permissions
from titan.tools.builtin import code as code_tools
from titan.tools.builtin import fs as fs_tools
from titan.tools.errors import ToolError
from titan.tools.registry import ToolExecutionContext
from titan.tools.sandbox.workspace import Workspace, WorkspaceManager

from .conftest import run_async

_ENV_SECRET = "DB_PASSWORD=hunter2-super-secret-value"
_RSA_SECRET = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nplanted-key-material\n-----END OPENSSH PRIVATE KEY-----"
)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    manager = WorkspaceManager(tmp_path / "root")
    ws = manager.for_call(uuid.uuid4(), uuid.uuid4())
    # The DoD repo: two planted secret files + ordinary code around them.
    (ws.root / ".env").write_text(_ENV_SECRET, encoding="utf-8")
    (ws.root / "id_rsa").write_text(_RSA_SECRET, encoding="utf-8")
    (ws.root / "src").mkdir()
    (ws.root / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8", newline="")
    (ws.root / "README.md").write_text("hello DB_PASSWORD docs\n", encoding="utf-8")
    (ws.root / "certs").mkdir()
    (ws.root / "certs" / "server.pem").write_text("PEM-CONTENT", encoding="utf-8")
    return ws


def _ctx(workspace: Workspace) -> ToolExecutionContext:
    return ToolExecutionContext(
        engine=None,  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        agent_id="agent.backend.1",
        agent_uuid=None,
        workspace=workspace,
    )


def _run(coro_fn: Any, args: dict[str, Any], ctx: ToolExecutionContext) -> Any:
    return run_async(lambda: coro_fn(args, ctx))


# --- fs.read -----------------------------------------------------------------


@pytest.mark.parametrize("path", [".env", "id_rsa", "certs/server.pem"])
def test_fs_read_refuses_secret_files(workspace: Workspace, path: str) -> None:
    with pytest.raises(ToolError) as err:
        _run(fs_tools._read, {"path": path}, _ctx(workspace))
    assert err.value.code == "PERMISSION_DENIED"
    assert "SEC-055" in err.value.message
    # The refusal must never quote the file's content.
    assert "hunter2" not in err.value.message
    assert "PRIVATE KEY" not in err.value.message


def test_fs_read_still_serves_ordinary_files(workspace: Workspace) -> None:
    out = _run(fs_tools._read, {"path": "src/main.py"}, _ctx(workspace))
    assert out.result["content"] == "print('hello')\n"


# --- fs.list / fs.glob ---------------------------------------------------------


def test_fs_list_omits_secret_files(workspace: Workspace) -> None:
    out = _run(fs_tools._list, {"path": "."}, _ctx(workspace))
    names = {entry["name"] for entry in out.result["entries"]}
    assert ".env" not in names
    assert "id_rsa" not in names
    assert {"src", "README.md", "certs"} <= names


def test_fs_glob_omits_secret_files(workspace: Workspace) -> None:
    out = _run(fs_tools._glob, {"pattern": "**/*"}, _ctx(workspace))
    matches = set(out.result["matches"])
    assert ".env" not in matches
    assert "id_rsa" not in matches
    assert "certs/server.pem" not in matches
    assert "src/main.py" in matches


def test_fs_glob_targeting_the_secret_directly_returns_nothing(workspace: Workspace) -> None:
    out = _run(fs_tools._glob, {"pattern": ".env*"}, _ctx(workspace))
    assert out.result["matches"] == []


# --- fs.grep -------------------------------------------------------------------


def test_fs_grep_never_matches_secret_file_content(workspace: Workspace) -> None:
    out = _run(fs_tools._grep, {"pattern": "DB_PASSWORD|PRIVATE KEY"}, _ctx(workspace))
    paths = {m["path"] for m in out.result["matches"]}
    assert paths == {"README.md"}  # the docs mention, never the .env line
    texts = " ".join(m["text"] for m in out.result["matches"])
    assert "hunter2" not in texts


# --- code.test report reader (caller-controlled paths) --------------------------


def test_code_report_reader_refuses_secret_paths(workspace: Workspace) -> None:
    class _Session:
        def __init__(self, ws: Workspace) -> None:
            self.workspace = ws

    session: Any = _Session(workspace)
    assert code_tools._read_workspace_file(session, ".env") is None
    assert code_tools._read_workspace_file(session, "id_rsa") is None
    assert code_tools._read_workspace_file(session, "src/main.py") == "print('hello')\n"


# --- plugin fs.read capability ---------------------------------------------------


def test_plugin_fs_read_denies_secret_paths_even_when_glob_allows(
    workspace: Workspace,
) -> None:
    broker = CapabilityBroker(
        "test.plugin",
        Permissions(filesystem=["**/*"]),  # manifest asks for EVERYTHING
        workspace_root=workspace.root,
    )

    async def read(path: str) -> Any:
        return await broker(jsonrpc.C_FS_READ, {"path": path})

    with pytest.raises(RpcError) as err:
        run_async(lambda: read(".env"))
    assert "SEC-055" in str(err.value.message)
    assert broker.denials and broker.denials[0][0] == "filesystem"
    assert run_async(lambda: read("src/main.py")) == "print('hello')\n"


def test_plugin_fs_read_denies_symlink_to_secret(workspace: Workspace) -> None:
    """Phase-69 review: the raw-name check misses a `report.txt -> .env`
    symlink; the resolved-path check must catch it (else the plugin reads the
    secret's content). Skips where the OS forbids unprivileged symlinks."""
    link = workspace.root / "report.txt"
    try:
        link.symlink_to(workspace.root / ".env")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")

    broker = CapabilityBroker(
        "test.plugin",
        Permissions(filesystem=["**/*"]),
        workspace_root=workspace.root,
    )

    with pytest.raises(RpcError) as err:
        run_async(lambda: broker(jsonrpc.C_FS_READ, {"path": "report.txt"}))
    assert "SEC-055" in str(err.value.message)
    assert _ENV_SECRET not in str(err.value.message)
