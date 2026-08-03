"""Real git-backed proof of TITAN-EVO-002 (Phase 83).

Uses an actual throwaway git repo (not a simulated byte-swap) to prove: (a) the
SHA-pinned reader loads an artifact's bytes at a pinned commit, and (b) a genuine
``git revert`` of an evolution change restores the prior prompt bytes byte-for-byte
and the reverted (previously approved) version resolves again.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from titan.evolution import gitref
from titan.evolution.manifest import content_sha256
from titan.evolution.resolver import EvolutionError, EvolutionResolver

REPO_ROOT = Path(__file__).resolve().parents[3]


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@titan.test",
            "-c",
            "user.name=titan-test",
            "-c",
            "commit.gpgsign=false",
            # Preserve LF on checkout: the deployment reads the LF-stored files
            # directly, so a global core.autocrlf must not fool the byte-for-byte
            # assertion (it would only reflect a Windows checkout filter, not drift).
            "-c",
            "core.autocrlf=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo).mkdir()
    shutil.copytree(REPO_ROOT / "evolution", repo / "evolution")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v1: genesis registry")
    return repo


def _repin(repo: Path, agent: str, new_bytes: bytes) -> str:
    """Write new prompt bytes + update the manifest's pinned sha for that agent;
    returns the new sha. (Simulates an approved EP that changes a prompt.)"""
    target = repo / "evolution" / "prompts" / agent / "system.md"
    target.write_bytes(new_bytes)
    new_sha = content_sha256(new_bytes)
    manifest = repo / "evolution" / "manifest.yaml"
    old_sha = content_sha256(
        (REPO_ROOT / "evolution" / "prompts" / agent / "system.md").read_bytes()
    )
    text = manifest.read_text(encoding="utf-8").replace(old_sha, new_sha)
    manifest.write_text(text, encoding="utf-8", newline="\n")
    return new_sha


def test_read_blob_loads_bytes_at_a_pinned_commit(git_repo: Path) -> None:
    path = "evolution/prompts/security/system.md"
    v1_sha = gitref.head_sha(git_repo)
    original = (git_repo / path).read_bytes()

    # Change + commit v2.
    (git_repo / path).write_bytes(original + b"\n{# v2 #}\n")
    _git(git_repo, "commit", "-qam", "v2")

    # The SHA-pinned reader still returns the v1 bytes at the v1 commit.
    assert gitref.read_blob(git_repo, v1_sha, path) == original
    # ...and HEAD now differs.
    assert gitref.read_blob(git_repo, "HEAD", path) != original


def test_git_revert_restores_prior_bytes_and_reapproves(git_repo: Path) -> None:
    agent = "security"
    path = git_repo / "evolution" / "prompts" / agent / "system.md"
    original = path.read_bytes()
    r0 = EvolutionResolver.load(git_repo / "evolution" / "prompts")
    v1_sha = r0.resolve_prompt(agent).sha256

    # An approved EP changes the prompt (bytes + manifest pin) and commits it.
    _repin(git_repo, agent, original + b"\n{# approved v2 change #}\n")
    _git(git_repo, "commit", "-qam", "v2: change security prompt")
    r_v2 = EvolutionResolver.load(git_repo / "evolution" / "prompts")
    assert r_v2.resolve_prompt(agent).sha256 != v1_sha  # v2 served

    # Genuine `git revert` of the v2 commit.
    _git(git_repo, "revert", "--no-edit", "HEAD")

    # Byte-for-byte restoration, and the reverted (v1) version resolves again.
    assert path.read_bytes() == original
    r_reverted = EvolutionResolver.load(git_repo / "evolution" / "prompts")
    assert r_reverted.resolve_prompt(agent).sha256 == v1_sha


def test_unsafe_git_ref_is_rejected(git_repo: Path) -> None:
    with pytest.raises(gitref.GitError, match="unsafe git ref"):
        gitref.read_blob(git_repo, "HEAD; rm -rf /", "evolution/manifest.yaml")


def test_resolver_over_reverted_tree_still_gates(git_repo: Path) -> None:
    # After a revert the tree is clean/approved; an *unapproved* working-tree edit
    # on top is still refused (the gate is per-resolve, not per-process).
    path = git_repo / "evolution" / "prompts" / "qa" / "system.md"
    path.write_bytes(path.read_bytes() + b"\n{# uncommitted drift #}\n")
    with pytest.raises(EvolutionError):
        EvolutionResolver.load(git_repo / "evolution" / "prompts").resolve_prompt("qa")
