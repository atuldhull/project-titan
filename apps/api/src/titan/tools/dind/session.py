"""Per-task DinD sessions — the rootless sidecar the ``docker.*`` tools run
against (Phase 22), pooled one-per-task like the code-exec sandbox.

A DinD sidecar is EXPENSIVE (a nested daemon + image seed), so the Tool Bus does
not create one per call. Each call gets a lightweight :class:`DindSession` bound
to (project, task, workspace); the sidecar is created lazily on the first
``await session.get()`` and REUSED by every later docker.* call in the same task
via :class:`DindSessionManager` (one sidecar per task). The orchestrator destroys
it at task end. Absent Docker → TOOL_UNAVAILABLE, fail closed."""

import asyncio
import uuid

from titan.tools.dind.controller import DindController, DindSidecar
from titan.tools.errors import ToolError
from titan.tools.sandbox.workspace import Workspace

_Key = tuple[str, str]
_LOCK_STRIPES = 64


class DindSessionManager:
    """A process-wide pool of per-task DinD sidecars."""

    def __init__(self, controller: DindController) -> None:
        self._controller = controller
        self._pool: dict[_Key, DindSidecar] = {}
        # Fixed lock stripe (not a per-key dict) so memory is O(stripes) forever.
        self._locks = [asyncio.Lock() for _ in range(_LOCK_STRIPES)]

    @staticmethod
    def _key(project_id: uuid.UUID, task_id: uuid.UUID | None) -> _Key:
        return (str(project_id), str(task_id) if task_id is not None else "adhoc")

    def _lock(self, key: _Key) -> asyncio.Lock:
        return self._locks[hash(key) % _LOCK_STRIPES]

    async def acquire(
        self, project_id: uuid.UUID, task_id: uuid.UUID | None, workspace: Workspace
    ) -> DindSidecar:
        key = self._key(project_id, task_id)
        async with self._lock(key):
            existing = self._pool.get(key)
            if existing is not None:
                return existing
            sidecar = await self._controller.create(task_id, workspace)
            self._pool[key] = sidecar
            return sidecar

    async def release(self, project_id: uuid.UUID, task_id: uuid.UUID | None) -> None:
        key = self._key(project_id, task_id)
        async with self._lock(key):
            sidecar = self._pool.pop(key, None)
        if sidecar is not None:
            await sidecar.destroy()

    async def aclose(self) -> None:
        sidecars = list(self._pool.values())
        self._pool.clear()
        for sidecar in sidecars:
            await sidecar.destroy()


class DindSession:
    """The per-call handle placed in ``ToolExecutionContext.dind``."""

    def __init__(
        self,
        manager: DindSessionManager,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
        workspace: Workspace,
    ) -> None:
        self._manager = manager
        self._project_id = project_id
        self._task_id = task_id
        self._workspace = workspace

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    async def get(self) -> DindSidecar:
        try:
            return await self._manager.acquire(self._project_id, self._task_id, self._workspace)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 — any daemon failure is graceful
            raise ToolError("TOOL_UNAVAILABLE", f"DinD unavailable: {exc}") from exc


__all__ = ["DindSession", "DindSessionManager"]
