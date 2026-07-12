"""Write the kit into a workspace, and activate the git hook.

Installation is idempotent and safe to re-run: the DevOps agent runs
`titan-ci-init` on a fresh workspace, and re-runs it after a kit upgrade to pull
new gates in. Re-running never clobbers a file the project legitimately owns —
see :func:`install_kit`.
"""

import stat
import subprocess  # noqa: S404 — fixed argv, no shell
from dataclasses import dataclass
from pathlib import Path

from titan.ci_kit.config import KitConfig
from titan.ci_kit.render import KitFile, render_kit


@dataclass(frozen=True)
class InstallResult:
    written: tuple[str, ...]
    skipped: tuple[str, ...]
    hooks_activated: bool

    def render(self) -> str:
        lines = [f"installed {len(self.written)} file(s) into the workspace"]
        lines.extend(f"  + {path}" for path in self.written)
        if self.skipped:
            lines.append(f"kept {len(self.skipped)} existing file(s) (pass --force to overwrite)")
            lines.extend(f"  = {path}" for path in self.skipped)
        lines.append(
            "activated .githooks (core.hooksPath)"
            if self.hooks_activated
            else "could not activate .githooks — not a git repository yet; after `git init` run:\n"
            "  git config core.hooksPath .githooks"
        )
        return "\n".join(lines)


def _write(root: Path, file: KitFile) -> None:
    target = root / file.path
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": the commit-msg hook is executed by /bin/sh, and a CRLF
    # shebang line makes the kernel fail with a bare "not found" that has sent
    # more than one team hunting for a missing interpreter.
    target.write_text(file.content, encoding="utf-8", newline="\n")
    if file.executable:
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _activate_hooks(root: Path) -> bool:
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "config", "core.hooksPath", ".githooks"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result.returncode == 0


def install_kit(workspace: Path, config: KitConfig, *, force: bool = False) -> InstallResult:
    """Drop every kit file into ``workspace`` and activate the commit-msg hook.

    ``ruff.toml`` and ``eslint.config.mjs`` are the project's own files once
    written — a project may legitimately add rules to them — so a re-run leaves
    them alone unless ``--force``. Everything under ``.titan/ci`` is the kit's,
    and is always overwritten: those files ARE the gates, and letting a
    stale-but-present copy survive a kit upgrade is how a workspace ends up
    enforcing a constitution nobody is reading any more.
    """
    workspace.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    skipped: list[str] = []
    for file in render_kit(config):
        kit_owned = file.path.startswith(".titan/")
        exists = (workspace / file.path).exists()
        if exists and not force and not kit_owned:
            skipped.append(file.path)
            continue
        _write(workspace, file)
        written.append(file.path)

    return InstallResult(
        written=tuple(written),
        skipped=tuple(skipped),
        hooks_activated=_activate_hooks(workspace),
    )


__all__ = ["InstallResult", "install_kit"]
