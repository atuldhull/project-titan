"""The Sandbox Controller — ephemeral hardened per-task containers (Vol 8 §5,
ADR-SEC-02, TITAN-SEC-040..044).

Each task gets a container that runs LLM-generated code (the red-team probes
here; terminal.run/code.* in Phase 21) under the full §5 hardening: non-root,
ALL caps dropped, no-new-privileges, read-only rootfs (writable ``/workspace``
bind + ``/tmp`` tmpfs), deny-all network, seccomp, and the cgroup ceiling. The
Docker socket and platform credentials are NEVER mounted (SEC-044). Runs
container-per-task so sequential tasks are isolated (SEC-040).

The docker SDK is synchronous; every daemon call is dispatched to a thread so
the async Tool Bus is never blocked (TITAN-BCK-002 / ruff ASYNC).
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import docker
import structlog
from docker.errors import DockerException, ImageNotFound, NotFound

from titan.tools.errors import ToolError
from titan.tools.sandbox.policy import ResourceLimits, SandboxPolicy
from titan.tools.sandbox.workspace import MOUNT_POINT, Workspace

logger = structlog.get_logger("titan.tools.sandbox")

# busybox/coreutils `timeout` enforces wall-clock INSIDE the sandbox regardless
# of the SDK; -s KILL guarantees a runaway can't outlive its budget.
_KILL_AFTER_S = 5


@dataclass(frozen=True)
class ExecResult:
    """The captured outcome of one argv execution in the sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    oom_killed: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.oom_killed


class Sandbox:
    """A live per-task container handle. Use via :class:`SandboxController`."""

    def __init__(self, client: docker.DockerClient, container_id: str, policy: SandboxPolicy):
        self._client = client
        self._id = container_id
        self._policy = policy

    @property
    def container_id(self) -> str:
        return self._id

    async def exec(
        self,
        argv: list[str],
        *,
        limits: ResourceLimits | None = None,
        workdir: str = MOUNT_POINT,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run ``argv`` (NEVER a shell string — TITAN-INT-031) in the sandbox,
        wrapped in an in-container hard timeout. Output is captured and capped
        per stream at the policy's ``max_output_bytes``."""
        budget = (limits or ResourceLimits()).clamped(self._policy)
        window = max(1, int(budget.timeout_s))
        wrapped = ["timeout", "-s", "KILL", "-k", str(_KILL_AFTER_S), str(window), *argv]
        started = time.monotonic()
        try:
            exit_code, out, err = await asyncio.to_thread(
                self._exec_blocking, wrapped, workdir, env
            )
        except NotFound as exc:  # container gone (destroyed / OOM-reaped)
            raise ToolError("SANDBOX_VIOLATION", "sandbox is no longer running") from exc
        except DockerException as exc:
            raise ToolError("UPSTREAM_ERROR", f"sandbox exec failed: {exc}") from exc
        elapsed = time.monotonic() - started
        # 124 = busybox timeout's own exit; 137/143 = SIGKILL/SIGTERM which the
        # kernel OOM-killer ALSO uses. Disambiguate by wall clock: a SIGKILL
        # landing well before the window elapsed is an OOM/forced kill, not a
        # wall-clock timeout.
        killed = exit_code in (124, 137, 143)
        timed_out = exit_code == 124 or (killed and elapsed >= window - 0.5)
        oom_killed = killed and not timed_out
        cap = self._policy.max_output_bytes
        return ExecResult(
            exit_code=exit_code,
            stdout=_cap(out, cap),
            stderr=_cap(err, cap),
            timed_out=timed_out,
            oom_killed=oom_killed,
        )

    def _exec_blocking(
        self, argv: list[str], workdir: str, env: dict[str, str] | None
    ) -> tuple[int, str, str]:
        container = self._client.containers.get(self._id)
        result = container.exec_run(
            cmd=argv,
            workdir=workdir,
            environment=env or {},
            user=self._policy.user,
            demux=True,
            tty=False,
        )
        stdout_b, stderr_b = result.output if result.output else (b"", b"")
        return (
            int(result.exit_code) if result.exit_code is not None else -1,
            (stdout_b or b"").decode("utf-8", "replace"),
            (stderr_b or b"").decode("utf-8", "replace"),
        )

    async def destroy(self) -> None:
        try:
            await asyncio.to_thread(self._destroy_blocking)
        except NotFound:
            return
        except DockerException as exc:
            logger.warning("sandbox.destroy_failed", container_id=self._id, error=str(exc))

    def _destroy_blocking(self) -> None:
        container = self._client.containers.get(self._id)
        container.remove(force=True)


class SandboxController:
    """Creates and destroys per-task sandboxes. One controller per process."""

    def __init__(self, policy: SandboxPolicy, *, docker_host: str = "") -> None:
        self._policy = policy
        self._docker_host = docker_host
        self._client: docker.DockerClient | None = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = (
                docker.DockerClient(base_url=self._docker_host)
                if self._docker_host
                else docker.from_env()
            )
        return self._client

    async def create(
        self,
        task_id: uuid.UUID | None,
        workspace: Workspace,
        *,
        policy: SandboxPolicy | None = None,
    ) -> Sandbox:
        """Start a hardened container with ``workspace`` bind-mounted rw at
        ``/workspace`` and nothing else writable. Raises TOOL_UNAVAILABLE if the
        daemon or base image is unreachable."""
        active = policy or self._policy
        try:
            container_id = await asyncio.to_thread(
                self._create_blocking, task_id, workspace.root, active
            )
        except ImageNotFound as exc:
            raise ToolError(
                "TOOL_UNAVAILABLE", f"sandbox image {active.image!r} is not available"
            ) from exc
        except DockerException as exc:
            raise ToolError("TOOL_UNAVAILABLE", f"sandbox daemon unavailable: {exc}") from exc
        return Sandbox(self._get_client(), container_id, active)

    def _create_blocking(
        self, task_id: uuid.UUID | None, workspace_root: Path, policy: SandboxPolicy
    ) -> str:
        client = self._get_client()
        name = f"titan-sbx-{task_id or uuid.uuid4()}"
        tmpfs = {path: f"rw,size={size},mode=1777" for path, size in policy.tmpfs.items()}
        container = client.containers.run(
            image=policy.image,
            # Keep the sandbox alive for the task; execs land via exec_run.
            command=["sleep", str(int(policy.wall_clock_s) + 60)],
            name=name,
            detach=True,
            remove=False,
            user=policy.user,
            working_dir=MOUNT_POINT,
            network_mode=policy.network_mode,
            read_only=policy.read_only_rootfs,
            cap_drop=["ALL"] if policy.drop_all_capabilities else [],
            security_opt=policy.security_opts(),
            mem_limit=policy.memory_bytes,
            nano_cpus=policy.nano_cpus,
            pids_limit=policy.pids_limit,
            tmpfs=tmpfs,
            # The ONLY writable bind — the task's workspace. No socket, no creds.
            volumes={str(workspace_root): {"bind": MOUNT_POINT, "mode": "rw"}},
            environment={},
            labels={"titan.sandbox": "1", "titan.task": str(task_id or "adhoc")},
        )
        return str(container.id)

    async def ping(self) -> bool:
        """True if the daemon is reachable (used to skip sandbox features when
        Docker is absent)."""
        try:
            return bool(await asyncio.to_thread(self._get_client().ping))
        except DockerException:
            return False


def _cap(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    truncated = text.encode("utf-8")[:limit].decode("utf-8", "ignore")
    return truncated + "\n…[output truncated]"


__all__ = ["ExecResult", "Sandbox", "SandboxController"]
