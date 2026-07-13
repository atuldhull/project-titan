"""The task-branch validator (Phase 48; TITAN-SEC-031 reconciled with Vol 6 §6.2).

Pure unit tests — no Docker, no DB. Proves that both the platform ``titan/task-*``
form and the Vol 6 §6.2 Conventional ``type/task-id-slug`` form are writable task
branches, while protected branches and ref-smuggling are always rejected (so the
SEC-031 guarantee survives the §6.2 generalization)."""

import pytest

from titan.tools.branches import is_task_branch


@pytest.mark.parametrize(
    "name",
    [
        "titan/task-1",
        "titan/task-42-invoice-pdf",
        "feat/TSK-142-invoice-pdf",
        "fix/TSK-9-null-deref",
        "chore/TSK-3-bump-deps",
        "docs/readme",
        "refactor/TSK-7",
        "ci/pipeline",
        "perf/hot-path",
        "test/TSK-5-edge-cases",
        "build/TSK-2-image",
        "revert/TSK-8",
        "style/format",
    ],
)
def test_accepts_titan_task_and_conventional_branches(name: str) -> None:
    assert is_task_branch(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "main",
        "master",
        "develop",
        "trunk",
        "production",
        "prod",
        "release",
        "hotfix",
        "HEAD",
        "MAIN",  # case-insensitive protected-name guard
        "release/1.0",  # 'release' is not a Conventional type
        "wip/whatever",  # not a Conventional type, not titan/task-*
        "feature/x",  # 'feature' is not a Conventional type ('feat' is)
        "just-a-branch",
    ],
)
def test_rejects_protected_and_non_task_branches(name: str) -> None:
    assert is_task_branch(name) is False


@pytest.mark.parametrize(
    "name",
    [
        "titan/task-1:main",  # refspec
        "feat/x:refs/heads/main",  # refspec on a Conventional branch
        "+titan/task-1",  # force
        "titan/task-1^",  # ref expression
        "titan/task-1~1",
        "feat/x@{upstream}",
        "titan/task-1 main",  # whitespace-smuggled second ref
        "feat/x\tmain",
    ],
)
def test_rejects_ref_smuggling(name: str) -> None:
    assert is_task_branch(name) is False
