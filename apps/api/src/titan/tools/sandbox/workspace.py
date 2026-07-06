"""Per-task workspace directory + the symlink-safe jail (TITAN-SEC-073).

Each task owns a host directory (created by :class:`WorkspaceManager`) that is
bind-mounted rw at ``/workspace`` in its sandbox. The fs.* tools operate on this
directory as trusted tool-host code, but every path they touch is resolved
through :meth:`Workspace.resolve`, which — unlike the string-level
``constraints.workspace_path`` — follows symlinks via ``realpath`` and re-checks
containment, defeating a symlink planted INSIDE the workspace that points at
``/etc/passwd``. A path that escapes raises ``SANDBOX_VIOLATION``.
"""

import os
import shutil
import uuid
from pathlib import Path

from titan.tools.errors import ToolError

# The in-sandbox mount point every workspace is presented as.
MOUNT_POINT = "/workspace"


class Workspace:
    """A single task's jailed workspace rooted at a real host directory."""

    def __init__(self, root: Path) -> None:
        # The canonical (symlink-free) root; all containment checks are against
        # this so the root itself can live under a symlinked temp dir.
        self._root = Path(os.path.realpath(root))

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, raw: str, *, must_exist: bool = False) -> Path:
        """Resolve ``raw`` (a /workspace-relative or absolute-in-sandbox path) to
        a real host path CONFINED to the workspace, following symlinks. Raises
        ``SANDBOX_VIOLATION`` on any escape, ``TOOL_UNAVAILABLE`` if
        ``must_exist`` and the target is missing."""
        if "\x00" in raw:
            raise ToolError("SANDBOX_VIOLATION", "path contains a NUL byte")
        rel = raw
        # Treat an absolute in-sandbox path as workspace-relative.
        for prefix in (MOUNT_POINT + "/", MOUNT_POINT):
            if rel == MOUNT_POINT:
                rel = ""
            elif rel.startswith(prefix):
                rel = rel[len(prefix) :]
                break
        rel = rel.lstrip("/")
        candidate = (self._root / rel) if rel else self._root

        # realpath collapses .. and resolves EVERY symlink component, so a
        # symlink inside the workspace pointing outside is caught here.
        resolved = Path(os.path.realpath(candidate))
        if resolved != self._root and self._root not in resolved.parents:
            raise ToolError("SANDBOX_VIOLATION", f"path {raw!r} escapes the workspace")
        # A not-yet-existing target: its PARENT must already resolve inside the
        # jail (so a symlinked parent can't redirect a create outside).
        if not resolved.exists():
            parent = Path(os.path.realpath(resolved.parent))
            if parent != self._root and self._root not in parent.parents:
                raise ToolError("SANDBOX_VIOLATION", f"path {raw!r} escapes the workspace")
        if must_exist and not resolved.exists():
            raise ToolError("TOOL_UNAVAILABLE", f"no such path in the workspace: {raw}")
        return resolved

    def relative(self, resolved: Path) -> str:
        """Render a resolved host path back as a /workspace-relative string."""
        return resolved.relative_to(self._root).as_posix() if resolved != self._root else "."


class WorkspaceManager:
    """Creates and destroys per-task workspace directories under a root
    (TITAN-SEC-040: distinct, clean dirs per task; destroyed after). Keyed by
    (project, task): a task gets its own dir, and task-less project work gets a
    per-project dir so two projects never collide."""

    def __init__(self, root: Path) -> None:
        # Store the root but DO NOT create it here — construction happens at boot
        # (under a read-only rootfs the root's parent may not be writable yet);
        # the per-task dir is created lazily on first use, which mkdirs the root.
        self._root = Path(root)

    @staticmethod
    def _name(project_id: uuid.UUID, task_id: uuid.UUID | None) -> str:
        return f"task-{task_id}" if task_id is not None else f"project-{project_id}"

    def for_call(self, project_id: uuid.UUID, task_id: uuid.UUID | None) -> Workspace:
        """The workspace the Tool Bus hands an executor — get-or-create (empty
        if new); never wipes an in-flight task's tree."""
        path = self._root / self._name(project_id, task_id)
        path.mkdir(parents=True, exist_ok=True)
        return Workspace(path)

    def reset(self, project_id: uuid.UUID, task_id: uuid.UUID | None) -> Workspace:
        """A fresh, empty workspace for a NEW task run (TITAN-SEC-040: sequential
        tasks get clean workspaces). Always wipes any prior tree."""
        path = self._root / self._name(project_id, task_id)
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return Workspace(path)

    def destroy(self, project_id: uuid.UUID, task_id: uuid.UUID | None) -> None:
        path = self._root / self._name(project_id, task_id)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


__all__ = ["MOUNT_POINT", "Workspace", "WorkspaceManager"]
