"""git.* sandbox tests (Phase 21; Vol 7 §4.1 & §5, TITAN-SEC-031).

Marked ``sandbox``: needs Docker + the titan-sandbox image. Proves the
branch → commit → merge flow inside the jail, the §4.1 git.commit contract, and
the TITAN-SEC-031 task-branch write constraint.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from titan.tools.builtin import git, terminal
from titan.tools.errors import ToolError

from ._sandbox_tools import ToolHarness, docker_ready
from .conftest import run_async

pytestmark = pytest.mark.sandbox


@pytest.fixture(scope="module", autouse=True)
def _require_docker() -> None:
    if not docker_ready():
        pytest.skip("no Docker / titan-sandbox:local image for the git suite")


@pytest.fixture
def repo() -> Iterator[ToolHarness]:
    h = ToolHarness()
    # A fresh repo on `main` with a base commit.
    run_async(
        lambda: terminal._run(
            {"argv": ["git", "-c", "safe.directory=*", "init", "-q", "-b", "main"]}, h.ctx()
        )
    )
    run_async(
        lambda: terminal._run(
            {
                "argv": [
                    "git",
                    "-c",
                    "safe.directory=*",
                    "-c",
                    "user.name=t",
                    "-c",
                    "user.email=t@t",
                    "commit",
                    "--allow-empty",
                    "-q",
                    "-m",
                    "base commit",
                ]
            },
            h.ctx(),
        )
    )
    try:
        yield h
    finally:
        h.close()


def _git(repo: ToolHarness, fn: Any, args: dict[str, Any]) -> Any:
    return run_async(lambda: fn(args, repo.ctx())).result


def test_branch_commit_merge_flow(repo: ToolHarness) -> None:
    # branch → commit on the task branch → merge back.
    created = _git(repo, git._branch, {"name": "titan/task-42"})
    assert created == {"created": True, "branch": "titan/task-42"}

    (repo.workspace.root / "feature.py").write_text("print('feature')\n")
    committed = _git(
        repo,
        git._commit,
        {"message": "add the feature module", "paths": ["feature.py"], "author_agent": "backend"},
    )
    assert len(committed["commit_sha"]) == 40 and committed["files_changed"] == 1

    # Switch to another task branch and merge the first into it.
    _git(repo, git._branch, {"name": "titan/task-99"})
    merged = _git(repo, git._merge, {"branch": "titan/task-42"})
    assert merged["merged"] is True
    assert (repo.workspace.root / "feature.py").exists()


def test_status_diff_log(repo: ToolHarness) -> None:
    _git(repo, git._branch, {"name": "titan/task-7"})
    (repo.workspace.root / "x.txt").write_text("content\n")
    status = _git(repo, git._status, {})
    assert status["branch"] == "titan/task-7" and status["clean"] is False
    assert any(f["path"] == "x.txt" for f in status["files"])

    _git(repo, git._commit, {"message": "commit x.txt now", "paths": ["x.txt"]})
    log = _git(repo, git._log, {"max_count": 10})
    assert any(c["subject"] == "commit x.txt now" for c in log["commits"])


@pytest.mark.parametrize("write", ["_commit", "_merge", "_rebase"])
def test_writes_on_non_task_branch_are_denied(repo: ToolHarness, write: str) -> None:
    # HEAD is `main` (not titan/task-*) → SEC-031 blocks EVERY current-branch
    # write (commit AND merge AND rebase), not just commit.
    (repo.workspace.root / "y.txt").write_text("y\n")
    by_tool: dict[str, dict[str, Any]] = {
        "_commit": {"message": "should be denied", "paths": ["y.txt"]},
        "_merge": {"branch": "titan/task-42"},
        "_rebase": {"onto": "titan/task-42"},
    }
    args = by_tool[write]
    with pytest.raises(ToolError) as exc:
        _git(repo, getattr(git, write), args)
    assert exc.value.code == "PERMISSION_DENIED" and "titan/task-" in exc.value.message


def test_first_commit_on_unborn_task_branch_is_allowed() -> None:
    # A fresh repo whose FIRST commit lands on an unborn titan/task-* branch must
    # not be falsely denied (the current-branch check uses branch --show-current,
    # defined on an unborn branch — rev-parse HEAD would print the literal 'HEAD').
    h = ToolHarness()
    try:
        run_async(
            lambda: terminal._run(
                {"argv": ["git", "-c", "safe.directory=*", "init", "-q", "-b", "titan/task-1"]},
                h.ctx(),
            )
        )
        (h.workspace.root / "app.py").write_text("print('hi')\n")
        committed = run_async(
            lambda: git._commit({"message": "initial import here", "paths": ["app.py"]}, h.ctx())
        ).result
        assert len(committed["commit_sha"]) == 40 and committed["files_changed"] == 1
    finally:
        h.close()


def test_branch_and_push_constraints_reject_refspec_smuggling() -> None:
    # The param constraints (checked in the bus) reject non-task names AND
    # refspec smuggling (titan/task-1:main) before the executor runs.
    from titan.tools.builtin.git import _remote_constraint, _task_branch_constraint

    rule = _task_branch_constraint("branch")
    assert rule({"branch": "titan/task-1"}) is None
    for bad in ["main", "titan/task-1:main", "titan/task-1:refs/heads/main", "+titan/task-1:main"]:
        verdict = rule({"branch": bad})
        assert verdict is not None and verdict[0] == "PERMISSION_DENIED", bad
    # A push remote may not be a URL / local path.
    assert _remote_constraint({"remote": "origin"}) is None
    denied = _remote_constraint({"remote": "https://evil.example/r.git"})
    assert denied is not None and denied[0] == "PERMISSION_DENIED"
