"""Per-task browser sessions — one connected Playwright page per task (Phase 22).

A browser container + Playwright connection is EXPENSIVE, and the page must
PERSIST across tool calls (``goto`` then ``snapshot`` then ``click`` operate on
the same page). So the pool keeps, per task, a live handle of
``{server, playwright, browser, page}``; the per-call :class:`BrowserSession`
just returns that shared page. Absent Docker → TOOL_UNAVAILABLE, fail closed."""

import asyncio
import uuid
from dataclasses import dataclass

import structlog
from playwright.async_api import Browser, Page, Playwright, async_playwright

from titan.tools.browser.controller import BrowserController, BrowserServer
from titan.tools.errors import ToolError

logger = structlog.get_logger("titan.tools.browser")

_Key = tuple[str, str]
_LOCK_STRIPES = 64


@dataclass
class _Handle:
    server: BrowserServer
    playwright: Playwright
    browser: Browser
    page: Page

    async def close(self) -> None:
        try:
            await self.browser.close()
            await self.playwright.stop()
        finally:
            await self.server.destroy()


class BrowserSessionManager:
    """A process-wide pool of per-task connected browser pages."""

    def __init__(self, controller: BrowserController) -> None:
        self._controller = controller
        self._pool: dict[_Key, _Handle] = {}
        self._locks = [asyncio.Lock() for _ in range(_LOCK_STRIPES)]

    @staticmethod
    def _key(project_id: uuid.UUID, task_id: uuid.UUID | None) -> _Key:
        return (str(project_id), str(task_id) if task_id is not None else "adhoc")

    def _lock(self, key: _Key) -> asyncio.Lock:
        return self._locks[hash(key) % _LOCK_STRIPES]

    async def acquire(self, project_id: uuid.UUID, task_id: uuid.UUID | None) -> Page:
        key = self._key(project_id, task_id)
        async with self._lock(key):
            existing = self._pool.get(key)
            if existing is not None:
                return existing.page
            handle = await self._connect(task_id)
            self._pool[key] = handle
            return handle.page

    async def _connect(self, task_id: uuid.UUID | None) -> _Handle:
        server = await self._controller.create(task_id)
        playwright: Playwright | None = None
        browser: Browser | None = None
        try:
            playwright = await async_playwright().start()
            browser = await playwright.chromium.connect(server.ws_endpoint)
            page = await browser.new_page()
        except Exception:
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()
            await server.destroy()
            raise
        return _Handle(server=server, playwright=playwright, browser=browser, page=page)

    async def release(self, project_id: uuid.UUID, task_id: uuid.UUID | None) -> None:
        key = self._key(project_id, task_id)
        async with self._lock(key):
            handle = self._pool.pop(key, None)
        if handle is not None:
            await handle.close()

    async def aclose(self) -> None:
        handles = list(self._pool.values())
        self._pool.clear()
        for handle in handles:
            await handle.close()


class BrowserSession:
    """The per-call handle placed in ``ToolExecutionContext.browser``."""

    def __init__(
        self,
        manager: BrowserSessionManager,
        *,
        project_id: uuid.UUID,
        task_id: uuid.UUID | None,
    ) -> None:
        self._manager = manager
        self._project_id = project_id
        self._task_id = task_id

    async def page(self) -> Page:
        try:
            return await self._manager.acquire(self._project_id, self._task_id)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 — any browser/daemon failure is graceful
            raise ToolError("TOOL_UNAVAILABLE", f"browser unavailable: {exc}") from exc


__all__ = ["BrowserSession", "BrowserSessionManager"]
