"""Shared helpers for the Phase-21 sandbox tool tests (terminal/git/code).

Builds a real per-task sandbox session against Docker + the tooled image, with a
fakeredis-backed output stream so streaming is exercised without a real broker.
"""

import tempfile
import uuid
from pathlib import Path

import fakeredis.aioredis

from titan.tools.registry import ToolExecutionContext
from titan.tools.sandbox.controller import SandboxController
from titan.tools.sandbox.policy import SandboxPolicy
from titan.tools.sandbox.session import SandboxSession, SandboxSessionManager
from titan.tools.sandbox.workspace import Workspace, WorkspaceManager
from titan.tools.streaming import ToolOutputStream

from .conftest import run_async

SANDBOX_IMAGE = "titan-sandbox:local"


def docker_ready() -> bool:
    try:
        return bool(run_async(SandboxController(SandboxPolicy(image=SANDBOX_IMAGE)).ping))
    except Exception:
        return False


class ToolHarness:
    """A live sandbox session + execution context for driving tool executors."""

    def __init__(self) -> None:
        self.controller = SandboxController(SandboxPolicy(image=SANDBOX_IMAGE))
        self.manager = SandboxSessionManager(self.controller)
        root = Path(tempfile.mkdtemp(prefix="titan-tools21-"))
        self.workspace: Workspace = WorkspaceManager(root).for_call(uuid.uuid4(), uuid.uuid4())
        self.project_id = uuid.uuid4()
        self.task_id = uuid.uuid4()
        self.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.stream = ToolOutputStream(
            self.redis,
            project_id=self.project_id,
            task_id=self.task_id,
            agent_id="agent.backend.1",
            tool="terminal.run",
            tool_call_id="tc_test",
        )
        self.session = SandboxSession(
            self.manager,
            project_id=self.project_id,
            task_id=self.task_id,
            workspace=self.workspace,
            stream=self.stream,
        )

    def ctx(self) -> ToolExecutionContext:
        return ToolExecutionContext(
            # terminal/git/code tools never touch the DB, so a null engine is fine.
            engine=None,  # type: ignore[arg-type]
            project_id=self.project_id,
            task_id=self.task_id,
            agent_id="agent.backend.1",
            agent_uuid=None,
            workspace=self.workspace,
            sandbox=self.session,
        )

    def close(self) -> None:
        run_async(self.manager.aclose)
