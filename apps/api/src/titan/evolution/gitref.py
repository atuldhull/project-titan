"""Read an evolution artifact at a pinned git commit on the HOST repo
(Vol 10 §2, TITAN-EVO-002 — "runtime loads artifacts by pinned SHA").

A tiny, host-only git reader used for SHA-pinned reads (verifying an approving
commit's bytes, or reconstructing a previously-approved version whose bytes are no
longer in the working tree). It mirrors the fixed-argv / no-shell / timeout /
typed-error subprocess pattern already used by the CI-kit diff-coverage gate — it
is NOT the sandbox git tool (that runs inside a task sandbox, agent-facing).
"""

import re
import subprocess  # noqa: S404 — git only, fixed argv, no shell (see below)
from pathlib import Path

# A git object-ish ref we are willing to interpolate into argv. Full/short SHAs
# and the symbolic HEAD only — never arbitrary user text (defence in depth on top
# of the fixed argv; a ':' or '..' can never reach the shell because there is none).
_REF_RE = re.compile(r"^(HEAD|[0-9a-fA-F]{7,40})$")

_TIMEOUT_S = 30.0


class GitError(RuntimeError):
    """A host git read failed (non-zero exit, missing git, or a bad ref/path)."""


def _validate_ref(ref: str) -> str:
    if not _REF_RE.match(ref):
        raise GitError(f"refusing unsafe git ref: {ref!r}")
    return ref


def head_sha(repo_root: Path) -> str:
    """The current HEAD commit SHA — stamped as ``commit_sha`` at approval time."""
    out = _run_text(repo_root, ["rev-parse", "HEAD"])
    return out.strip()


def read_blob(repo_root: Path, commit_sha: str, path: str) -> bytes:
    """The bytes of ``path`` as of ``commit_sha`` (``git show <sha>:<path>``).

    ``path`` is a repo-relative POSIX path; it is passed as a single fixed argv
    element after the validated ref, so there is no shell and no injection surface.
    """
    ref = _validate_ref(commit_sha)
    return _run_bytes(repo_root, ["show", f"{ref}:{path}"])


def _run_text(repo_root: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_TIMEOUT_S,
        )
    except FileNotFoundError as exc:  # git not installed
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out") from exc
    if result.returncode != 0:
        raise GitError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _run_bytes(repo_root: Path, args: list[str]) -> bytes:
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            check=False,
            timeout=_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out") from exc
    if result.returncode != 0:
        raise GitError(f"git {args[0]} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


__all__ = ["GitError", "head_sha", "read_blob"]
