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
import codecs
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import docker
import structlog
from docker.errors import DockerException, ImageNotFound, NotFound

from titan.tools.egress.tokens import mint_task_token
from titan.tools.errors import ToolError
from titan.tools.sandbox.policy import (
    TASK_DEFAULT_WALL_CLOCK_S,
    EgressConfig,
    ResourceLimits,
    SandboxPolicy,
)
from titan.tools.sandbox.workspace import MOUNT_POINT, Workspace

logger = structlog.get_logger("titan.tools.sandbox")

# busybox/coreutils `timeout` enforces wall-clock INSIDE the sandbox regardless
# of the SDK; -s KILL guarantees a runaway can't outlive its budget.
_KILL_AFTER_S = 5

_TRUNCATION_MARKER = "\n…[output truncated at cap]\n"

# An async sink for incremental output: (stream_name, chunk_text). Used to
# stream tool.output over Redis pub/sub (TITAN-INT-031).
ChunkSink = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True)
class ExecResult:
    """The captured outcome of one argv execution in the sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    oom_killed: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False

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

    async def exec_streamed(
        self,
        argv: list[str],
        on_chunk: ChunkSink,
        *,
        limits: ResourceLimits | None = None,
        workdir: str = MOUNT_POINT,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Like :meth:`exec` but streams each output chunk to ``on_chunk`` as it
        arrives (TITAN-INT-031). Output is still captured + capped per stream at
        the policy's ``max_output_bytes`` with truncation flagged; once a stream
        is truncated no further chunks are published (a runaway can't flood the
        dashboard) but draining continues so the process can be killed on time."""
        budget = (limits or ResourceLimits()).clamped(self._policy)
        window = max(1, int(budget.timeout_s))
        wrapped = ["timeout", "-s", "KILL", "-k", str(_KILL_AFTER_S), str(window), *argv]
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
        started = time.monotonic()

        def worker() -> None:
            try:
                api = self._client.api
                exec_id = api.exec_create(
                    self._id,
                    cmd=wrapped,
                    workdir=workdir,
                    user=self._policy.user,
                    environment=env or {},
                    tty=False,
                )["Id"]
                for stdout_b, stderr_b in api.exec_start(exec_id, stream=True, demux=True):
                    if stdout_b:
                        loop.call_soon_threadsafe(queue.put_nowait, ("stdout", stdout_b))
                    if stderr_b:
                        loop.call_soon_threadsafe(queue.put_nowait, ("stderr", stderr_b))
                code = api.exec_inspect(exec_id).get("ExitCode")
                loop.call_soon_threadsafe(queue.put_nowait, ("__done__", code))
            except Exception as exc:  # noqa: BLE001 — surfaced to the awaiter
                loop.call_soon_threadsafe(queue.put_nowait, ("__error__", exc))

        pump = asyncio.create_task(asyncio.to_thread(worker))
        cap = self._policy.max_output_bytes
        bufs = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}
        # Per-stream incremental UTF-8 decoders so a multibyte char split across
        # docker frames isn't force-replaced with U+FFFD mid-character.
        decoders = {
            "stdout": codecs.getincrementaldecoder("utf-8")("replace"),
            "stderr": codecs.getincrementaldecoder("utf-8")("replace"),
        }
        exit_code, error = -1, None
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "__done__":
                    exit_code = int(payload) if isinstance(payload, int) else -1
                    break
                if kind == "__error__":
                    error = payload
                    break
                chunk = payload if isinstance(payload, bytes | bytearray) else b""
                buf = bufs[kind]
                if len(buf) < cap:
                    # Capture AND publish the SAME clipped slice, so the live
                    # stream honors the per-stream cap just like the capture.
                    allowed = bytes(chunk[: cap - len(buf)])
                    buf.extend(allowed)
                    if not truncated[kind]:
                        await on_chunk(kind, decoders[kind].decode(allowed))
                    if len(buf) >= cap:  # just crossed the cap
                        truncated[kind] = True
                        await on_chunk(kind, _TRUNCATION_MARKER)
        finally:
            await pump
        if isinstance(error, BaseException):
            if isinstance(error, NotFound):
                raise ToolError("SANDBOX_VIOLATION", "sandbox is no longer running") from error
            raise ToolError("UPSTREAM_ERROR", f"sandbox exec failed: {error}") from error
        elapsed = time.monotonic() - started
        killed = exit_code in (124, 137, 143)
        timed_out = exit_code == 124 or (killed and elapsed >= window - 0.5)
        return ExecResult(
            exit_code=exit_code,
            stdout=bufs["stdout"].decode("utf-8", "replace"),
            stderr=bufs["stderr"].decode("utf-8", "replace"),
            timed_out=timed_out,
            oom_killed=killed and not timed_out,
            stdout_truncated=truncated["stdout"],
            stderr_truncated=truncated["stderr"],
        )

    async def start_background(
        self,
        argv: list[str],
        *,
        handle: str,
        workdir: str = MOUNT_POINT,
        env: dict[str, str] | None = None,
    ) -> int:
        """Launch ``argv`` detached, its output redirected to files under the
        workspace's ``.titan/bg/<handle>/`` dir; returns the container-namespace
        PID (which :meth:`signal` targets). The user command is passed as
        positional args to a FIXED launcher, so no shell interpolation of
        model-controlled input occurs (TITAN-INT-031)."""
        bg_dir = f"{MOUNT_POINT}/.titan/bg/{handle}"
        launcher = f'mkdir -p "{bg_dir}"; nohup "$@" >"{bg_dir}/out" 2>"{bg_dir}/err" & echo $!'
        result = await self.exec(
            ["sh", "-c", launcher, "titan-bg", *argv],
            workdir=workdir,
            env=env,
            limits=ResourceLimits(timeout_s=15),
        )
        pid_text = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if not pid_text.isdigit():
            raise ToolError(
                "UPSTREAM_ERROR", f"could not launch background process: {result.stderr}"
            )
        return int(pid_text)

    async def signal(self, pid: int, sig: str = "TERM") -> bool:
        """Send ``sig`` to a background PID; True if the process was there."""
        result = await self.exec(["kill", f"-{sig}", str(pid)], limits=ResourceLimits(timeout_s=10))
        return result.exit_code == 0

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

    def __init__(
        self, policy: SandboxPolicy, *, docker_host: str = "", egress: EgressConfig | None = None
    ) -> None:
        self._policy = policy
        self._docker_host = docker_host
        # When set, the sandbox joins the egress network + is pointed through the
        # forward proxy via a per-task credential (Phase 23); otherwise deny-all.
        self._egress = egress
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
        project_id: uuid.UUID | None = None,
    ) -> Sandbox:
        """Start a hardened container with ``workspace`` bind-mounted rw at
        ``/workspace`` and nothing else writable. When egress is configured the
        sandbox joins the egress network + is pointed through the forward proxy
        via a per-task credential. Raises TOOL_UNAVAILABLE if the daemon or base
        image is unreachable."""
        active = policy or self._policy
        try:
            container_id = await asyncio.to_thread(
                self._create_blocking, task_id, workspace.root, active, project_id
            )
        except ImageNotFound as exc:
            raise ToolError(
                "TOOL_UNAVAILABLE", f"sandbox image {active.image!r} is not available"
            ) from exc
        except DockerException as exc:
            raise ToolError("TOOL_UNAVAILABLE", f"sandbox daemon unavailable: {exc}") from exc
        return Sandbox(self._get_client(), container_id, active)

    def _create_blocking(
        self,
        task_id: uuid.UUID | None,
        workspace_root: Path,
        policy: SandboxPolicy,
        project_id: uuid.UUID | None,
    ) -> str:
        client = self._get_client()
        name = f"titan-sbx-{task_id or uuid.uuid4()}"
        tmpfs = {path: f"rw,size={size},mode=1777" for path, size in policy.tmpfs.items()}
        # Deny-all by default; if egress is wired, join the egress network and
        # inject the proxy/mirror env (deny-all still holds at the network layer —
        # the only reachable peer on an internal egress network is the proxy).
        network = self._egress.network if self._egress is not None else policy.network_mode
        environment = self._egress_env(project_id, task_id) if self._egress is not None else {}
        container = client.containers.run(
            image=policy.image,
            # Keep the sandbox alive for the task; execs land via exec_run.
            command=["sleep", str(int(policy.wall_clock_s) + 60)],
            name=name,
            detach=True,
            remove=False,
            user=policy.user,
            working_dir=MOUNT_POINT,
            network_mode=network,
            read_only=policy.read_only_rootfs,
            cap_drop=["ALL"] if policy.drop_all_capabilities else [],
            security_opt=policy.security_opts(),
            mem_limit=policy.memory_bytes,
            nano_cpus=policy.nano_cpus,
            pids_limit=policy.pids_limit,
            tmpfs=tmpfs,
            # The ONLY writable bind — the task's workspace. No socket, no creds.
            volumes={str(workspace_root): {"bind": MOUNT_POINT, "mode": "rw"}},
            environment=environment,
            labels={"titan.sandbox": "1", "titan.task": str(task_id or "adhoc")},
        )
        return str(container.id)

    def _egress_env(
        self, project_id: uuid.UUID | None, task_id: uuid.UUID | None
    ) -> dict[str, str]:
        """Proxy + mirror env for a proxied sandbox — the per-task credential is
        carried in the proxy URL's userinfo so pip/npm/git/curl authenticate to
        the forward proxy with no extra config."""
        egress = self._egress
        assert egress is not None  # guarded by the caller
        # The token carries the identity (for audit) + the task's reachable hosts:
        # the configured git-remote hosts here, and any Admin-approved hosts the
        # forward proxy adds from project_egress_hosts on the off-baseline path.
        token = mint_task_token(
            egress.secret,
            project_id=project_id or uuid.uuid4(),
            task_id=task_id,
            agent_id="sandbox",
            approved_hosts=egress.git_hosts,
            ttl_seconds=TASK_DEFAULT_WALL_CLOCK_S + 300.0,
        )
        proxy_url = f"http://task:{token}@{egress.proxy_host}:{egress.proxy_port}"
        env = {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "ALL_PROXY": proxy_url,
            "all_proxy": proxy_url,
            # Never proxy in-sandbox loopback traffic.
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost,127.0.0.1",
        }
        if egress.pip_index_url:
            env["PIP_INDEX_URL"] = egress.pip_index_url
            env["PIP_TRUSTED_HOST"] = _host_of(egress.pip_index_url)
        if egress.npm_registry_url:
            env["NPM_CONFIG_REGISTRY"] = egress.npm_registry_url
        return env

    async def ping(self) -> bool:
        """True if the daemon is reachable (used to skip sandbox features when
        Docker is absent)."""
        try:
            return bool(await asyncio.to_thread(self._get_client().ping))
        except DockerException:
            return False


def _host_of(url: str) -> str:
    """The bare host of a URL (for PIP_TRUSTED_HOST, which wants host[:port])."""
    authority = url.split("//", 1)[-1].split("/", 1)[0]
    return authority.split("@", 1)[-1].split(":", 1)[0]


def _cap(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    truncated = text.encode("utf-8")[:limit].decode("utf-8", "ignore")
    return truncated + "\n…[output truncated]"


__all__ = ["ChunkSink", "ExecResult", "Sandbox", "SandboxController"]
