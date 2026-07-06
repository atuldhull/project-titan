"""Per-task sandbox sessions — the running container the terminal/git/code tools
execute inside (Phase 21).

A per-task sandbox container is EXPENSIVE to create, so the Tool Bus does not
create one for every call (fs.* tools never need it). Instead the bus hands each
call a lightweight :class:`SandboxSession` bound to (project, task, workspace);
the container is created lazily on the FIRST ``await session.get()`` and REUSED
by every later call in the same task via the :class:`SandboxSessionManager` pool
(TITAN-SEC-040: one sandbox per task). The orchestrator destroys the session at
task end.

If no Docker daemon is reachable the manager is absent and code-exec tools fail
closed (TOOL_UNAVAILABLE) — exactly like the fs tools without a workspace.
"""

import asyncio
import uuid

from titan.tools.errors import ToolError
from titan.tools.sandbox.controller import Sandbox, SandboxController
from titan.tools.sandbox.workspace import Workspace
from titan.tools.streaming import ToolOutputStream

_Key = tuple[str, str]
_LOCK_STRIPES = 64


class SandboxSessionManager:
    """A process-wide pool of per-task sandbox containers."""

    def __init__(self, controller: SandboxController) -> None:
        self._controller = controller
        self._pool: dict[_Key, Sandbox] = {}
        # A FIXED stripe of locks (not a per-key dict) so serialization memory
        # is O(stripes) forever — a per-key defaultdict would leak one lock per
        # task for the process lifetime. Two different keys may briefly share a
        # stripe; that only costs a rare, harmless serialization.
        self._locks = [asyncio.Lock() for _ in range(_LOCK_STRIPES)]

    @staticmethod
    def _key(project_id: uuid.UUID, task_id: uuid.UUID | None) -> _Key:
        return (str(project_id), str(task_id) if task_id is not None else "adhoc")

    def _lock(self, key: _Key) -> asyncio.Lock:
        return self._locks[hash(key) % _LOCK_STRIPES]

    async def acquire(
        self, project_id: uuid.UUID, task_id: uuid.UUID | None, workspace: Workspace
    ) -> Sandbox:
        """Get-or-create the task's running sandbox. The per-key lock makes two
        concurrent tool calls in the same task share ONE container."""
        key = self._key(project_id, task_id)
        async with self._lock(key):
            existing = self._pool.get(key)
            if existing is not None:
                return existing
            sandbox = await self._controller.create(task_id, workspace)
            self._pool[key] = sandbox
            return sandbox

    async def release(self, project_id: uuid.UUID, task_id: uuid.UUID | None) -> None:
        """Destroy the task's sandbox (called at task end)."""
        key = self._key(project_id, task_id)
        async with self._lock(key):
            sandbox = self._pool.pop(key, None)
        if sandbox is not None:
            await sandbox.destroy()

    async def aclose(self) -> None:
        """Tear down every live sandbox (process shutdown)."""
        sandboxes = list(self._pool.values())
        self._pool.clear()
        for sandbox in sandboxes:
            await sandbox.destroy()


class SandboxSession:
    """The per-call handle placed in ``ToolExecutionContext.sandbox``."""

    def __init__(
        self,
        manager: SandboxSessionManager,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
        workspace: Workspace,
        stream: ToolOutputStream,
    ) -> None:
        self._manager = manager
        self._project_id = project_id
        self._task_id = task_id
        self._workspace = workspace
        self.stream = stream

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    async def get(self) -> Sandbox:
        """The task's running sandbox (created lazily, reused thereafter).
        Raises TOOL_UNAVAILABLE if the daemon/image is unreachable."""
        try:
            return await self._manager.acquire(self._project_id, self._task_id, self._workspace)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 — any daemon failure is graceful
            raise ToolError("TOOL_UNAVAILABLE", f"sandbox unavailable: {exc}") from exc


__all__ = ["SandboxSession", "SandboxSessionManager"]
