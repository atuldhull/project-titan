"""Shared helpers for the Phase-22 container tool tests (docker.* / browser.*).

Builds real per-task DinD / browser sessions against Docker + the tooled images,
mirroring ``_sandbox_tools.py``. The DinD sidecar auto-seeds ``titan-sandbox:local``
so compose/build fixtures start FROM it with no registry egress.
"""

import tempfile
import uuid
from pathlib import Path

from titan.tools.browser.controller import BrowserController
from titan.tools.browser.session import BrowserSession, BrowserSessionManager
from titan.tools.dind.controller import DindController
from titan.tools.dind.session import DindSession, DindSessionManager
from titan.tools.registry import ToolExecutionContext
from titan.tools.sandbox.workspace import Workspace, WorkspaceManager

from .conftest import run_async

DIND_IMAGE = "docker:dind-rootless"
BROWSER_IMAGE = "titan-browser:local"
SEED_IMAGE = "titan-sandbox:local"


def dind_ready() -> bool:
    try:
        return bool(run_async(DindController(image=DIND_IMAGE, preloaded_images=[]).ping))
    except Exception:
        return False


def browser_ready() -> bool:
    try:
        return bool(run_async(BrowserController(image=BROWSER_IMAGE).ping))
    except Exception:
        return False


class DindHarness:
    """A live per-task DinD session + execution context for docker.* executors."""

    def __init__(self) -> None:
        self.controller = DindController(image=DIND_IMAGE, preloaded_images=[SEED_IMAGE])
        self.manager = DindSessionManager(self.controller)
        root = Path(tempfile.mkdtemp(prefix="titan-docker22-"))
        self.project_id = uuid.uuid4()
        self.task_id = uuid.uuid4()
        self.workspace: Workspace = WorkspaceManager(root).for_call(self.project_id, self.task_id)
        self.session = DindSession(
            self.manager,
            project_id=self.project_id,
            task_id=self.task_id,
            workspace=self.workspace,
        )

    def ctx(self) -> ToolExecutionContext:
        return ToolExecutionContext(
            engine=None,  # type: ignore[arg-type]
            project_id=self.project_id,
            task_id=self.task_id,
            agent_id="agent.backend.1",
            agent_uuid=None,
            workspace=self.workspace,
            dind=self.session,
        )

    def close(self) -> None:
        run_async(self.manager.aclose)


class BrowserHarness:
    """A live per-task browser session + execution context for browser.* executors."""

    def __init__(self) -> None:
        self.controller = BrowserController(image=BROWSER_IMAGE)
        self.manager = BrowserSessionManager(self.controller)
        self.project_id = uuid.uuid4()
        self.task_id = uuid.uuid4()
        self.session = BrowserSession(
            self.manager, project_id=self.project_id, task_id=self.task_id
        )

    def ctx(self) -> ToolExecutionContext:
        return ToolExecutionContext(
            engine=None,  # type: ignore[arg-type]
            project_id=self.project_id,
            task_id=self.task_id,
            agent_id="agent.qa.1",
            agent_uuid=None,
            browser=self.session,
        )

    def close(self) -> None:
        run_async(self.manager.aclose)
