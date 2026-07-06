"""The DinD Controller — per-task rootless Docker-in-Docker sidecars (Phase 22;
Vol 7 §4.3, TITAN-SEC-042/044).

``docker.*`` tools must talk to a rootless sidecar daemon, NEVER the host daemon
socket (SEC-044). This controller creates that sidecar via the host daemon (the
trusted Sandbox-Controller role), with:

* ``--privileged`` on the OUTER container only — required so the inner dockerd
  can set up its user namespace; the inner daemon itself still runs rootless
  (uid 1000), so a break-in lands on an unprivileged nested daemon, not the host.
* the task workspace bind-mounted rw at ``/workspace`` (the compose files and
  build contexts the tools operate on) and NO docker socket / credentials.
* a fixed set of pre-published ports so a service the sidecar starts (via
  ``docker run -p`` / ``compose up``) is reachable from the host through the
  port-forward chain host:ephemeral → sidecar:APP_PORT → container.
* an offline image seed (host ``save`` | sidecar ``load``) so builds can start
  ``FROM titan-sandbox:local`` with zero registry egress (offline-first).

The docker SDK is synchronous; every daemon call runs in a thread so the async
Tool Bus is never blocked. Exec inside the sidecar reuses :class:`Sandbox` (the
same capped/streamed exec engine) pointed at the sidecar container.
"""

import asyncio
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import docker
import structlog
from docker.errors import DockerException, ImageNotFound

from titan.tools.errors import ToolError
from titan.tools.sandbox.controller import ChunkSink, ExecResult, Sandbox
from titan.tools.sandbox.policy import ResourceLimits, SandboxPolicy
from titan.tools.sandbox.workspace import Workspace

logger = structlog.get_logger("titan.tools.dind")

# The rootless daemon's socket inside the sidecar (rootlesskit remaps root→1000).
_DIND_SOCK = "unix:///run/user/1000/docker.sock"
# Exec inside the sidecar as its default user (the rootless uid 1000), not the
# hardened sandbox uid — the daemon socket is owned by 1000.
_DIND_EXEC_USER = ""
# Ports pre-published on the sidecar; a service must publish to one of these to
# be host-reachable. Kept small and well-known (compose fixtures target 8080).
APP_PORTS: tuple[int, ...] = (8080, 8081, 8082, 3000, 5000)
_WORKSPACE_MOUNT = "/workspace"
_SEED_MOUNT = "/titan-seed"
_READY_TIMEOUT_S = 45.0
# A bounded (not unlimited) PID ceiling on the sidecar so a fork bomb in a build
# or run step can't exhaust host PIDs (TITAN-SEC-042 'without host impact'). It
# is generous — a dockerd + buildkit + containerd + nested containers needs far
# more than the code-exec sandbox's 256 — but finite, which is what matters.
_SIDECAR_PIDS_LIMIT = 8192


class DindSidecar:
    """A live per-task rootless-DinD sidecar. ``exec_docker`` runs the docker CLI
    against its inner daemon; ``host_port`` maps an inner published port to the
    host port it is reachable on."""

    def __init__(
        self,
        client: docker.DockerClient,
        container_id: str,
        *,
        port_map: dict[int, int],
        seed_dir: Path,
        exec_policy: SandboxPolicy,
    ) -> None:
        self._client = client
        self._id = container_id
        self._port_map = port_map
        self._seed_dir = seed_dir
        # Reuse the capped/streamed exec engine, pointed at the sidecar.
        self._sbx = Sandbox(client, container_id, exec_policy)

    @property
    def container_id(self) -> str:
        return self._id

    def host_port(self, inner_port: int) -> int | None:
        """The host port an inner published ``inner_port`` is reachable on."""
        return self._port_map.get(inner_port)

    def endpoint(self, inner_port: int) -> str | None:
        host = self.host_port(inner_port)
        return f"http://localhost:{host}" if host is not None else None

    async def exec_docker(
        self,
        argv: list[str],
        *,
        workdir: str = _WORKSPACE_MOUNT,
        timeout_s: float = 300.0,
        on_chunk: ChunkSink | None = None,
    ) -> ExecResult:
        """Run ``docker <argv>`` inside the sidecar against its rootless daemon.
        Streams chunks to ``on_chunk`` when given (live build/compose output)."""
        cmd = ["docker", *argv]
        env = {"DOCKER_HOST": _DIND_SOCK}
        limits = ResourceLimits(timeout_s=timeout_s)
        if on_chunk is not None:
            return await self._sbx.exec_streamed(
                cmd, on_chunk, limits=limits, workdir=workdir, env=env
            )
        return await self._sbx.exec(cmd, limits=limits, workdir=workdir, env=env)

    async def wait_ready(self) -> None:
        """Block until the inner daemon answers, or raise TOOL_UNAVAILABLE. The
        deadline is wall-clock (time.monotonic), so a probe that hangs near its
        own 8s exec timeout can't stretch the effective wait far past
        ``_READY_TIMEOUT_S``."""
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            result = await self.exec_docker(
                ["version", "--format", "{{.Server.Version}}"], timeout_s=8
            )
            if result.ok and result.stdout.strip():
                return
            await asyncio.sleep(1.5)
        raise ToolError("TOOL_UNAVAILABLE", "the DinD sidecar daemon did not become ready")

    async def seed_images(self, names: list[str]) -> None:
        """Load host images into the sidecar's daemon offline (save | load), so
        builds can start FROM them with no registry egress. Fully best-effort:
        NO failure here (missing image, save/load hiccup) propagates — seeding a
        convenience must never tear down an otherwise-healthy sidecar."""
        for name in names:
            tar_name = _safe_tar_name(name)
            try:
                await asyncio.to_thread(self._save_blocking, name)
                try:
                    result = await self.exec_docker(
                        ["load", "-i", f"{_SEED_MOUNT}/{tar_name}"], timeout_s=180
                    )
                    if not result.ok:
                        logger.warning(
                            "dind.seed_load_failed", image=name, stderr=result.stderr[:400]
                        )
                finally:
                    (self._seed_dir / tar_name).unlink(missing_ok=True)
            except ImageNotFound:
                logger.warning("dind.seed_skipped_missing", image=name)
            except Exception as exc:  # noqa: BLE001 — best-effort; keep the sidecar
                logger.warning("dind.seed_failed", image=name, error=str(exc))

    def _save_blocking(self, name: str) -> None:
        image = self._client.images.get(name)  # raises ImageNotFound if absent
        tar_path = self._seed_dir / _safe_tar_name(name)
        with tar_path.open("wb") as fh:
            for chunk in image.save(named=True):
                fh.write(chunk)

    async def destroy(self) -> None:
        await self._sbx.destroy()
        await asyncio.to_thread(shutil.rmtree, self._seed_dir, True)


class DindController:
    """Creates and destroys per-task rootless-DinD sidecars. One per process."""

    def __init__(
        self,
        *,
        image: str,
        preloaded_images: list[str],
        docker_host: str = "",
        policy: SandboxPolicy | None = None,
    ) -> None:
        self._image = image
        self._preloaded = list(preloaded_images)
        self._docker_host = docker_host
        # The sidecar inherits the §5 CPU/mem ceiling (SEC-042) plus a bounded
        # (not unlimited) PID ceiling — see _SIDECAR_PIDS_LIMIT.
        self._policy = policy or SandboxPolicy()
        self._client: docker.DockerClient | None = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = (
                docker.DockerClient(base_url=self._docker_host)
                if self._docker_host
                else docker.from_env()
            )
        return self._client

    async def ping(self) -> bool:
        try:
            return bool(await asyncio.to_thread(self._get_client().ping))
        except DockerException:
            return False

    async def create(self, task_id: uuid.UUID | None, workspace: Workspace) -> DindSidecar:
        """Start a rootless-DinD sidecar for ``task_id`` with ``workspace`` mounted,
        wait for its daemon, and seed the preloaded images. Raises TOOL_UNAVAILABLE
        if the host daemon or DinD image is unreachable."""
        seed_dir = Path(tempfile.mkdtemp(prefix="titan-dind-seed-"))
        try:
            container_id, port_map = await asyncio.to_thread(
                self._create_blocking, task_id, workspace.root, seed_dir
            )
        except ImageNotFound as exc:
            shutil.rmtree(seed_dir, ignore_errors=True)
            raise ToolError(
                "TOOL_UNAVAILABLE", f"DinD image {self._image!r} is not available"
            ) from exc
        except DockerException as exc:
            shutil.rmtree(seed_dir, ignore_errors=True)
            raise ToolError("TOOL_UNAVAILABLE", f"DinD daemon unavailable: {exc}") from exc
        # The exec policy has no forced user (rootless default) and a generous
        # wall clock for builds; output is still capped per stream.
        exec_policy = self._policy.with_overrides(
            image=self._image, user=_DIND_EXEC_USER, network_mode="bridge"
        )
        sidecar = DindSidecar(
            self._get_client(),
            container_id,
            port_map=port_map,
            seed_dir=seed_dir,
            exec_policy=exec_policy,
        )
        try:
            await sidecar.wait_ready()
            await sidecar.seed_images(self._preloaded)
        except Exception:
            await sidecar.destroy()
            raise
        return sidecar

    def _create_blocking(
        self, task_id: uuid.UUID | None, workspace_root: Path, seed_dir: Path
    ) -> tuple[str, dict[int, int]]:
        client = self._get_client()
        name = f"titan-dind-{task_id or uuid.uuid4()}"
        container = client.containers.run(
            image=self._image,
            command=["--tls=false"],  # dockerd-entrypoint.sh args: plain unix socket
            name=name,
            detach=True,
            remove=False,
            privileged=True,  # OUTER only; the inner daemon stays rootless (uid 1000)
            environment={"DOCKER_TLS_CERTDIR": ""},
            volumes={
                str(workspace_root): {"bind": _WORKSPACE_MOUNT, "mode": "rw"},
                str(seed_dir): {"bind": _SEED_MOUNT, "mode": "ro"},
            },
            ports={f"{p}/tcp": None for p in APP_PORTS},  # ephemeral host ports
            mem_limit=self._policy.memory_bytes,
            nano_cpus=self._policy.nano_cpus,
            pids_limit=_SIDECAR_PIDS_LIMIT,  # bounded fork-bomb ceiling (SEC-042)
            labels={"titan.dind": "1", "titan.task": str(task_id or "adhoc")},
        )
        # If reading the port map fails, the container is already running — force
        # it away so a post-run error can't orphan a privileged sidecar.
        try:
            container.reload()
            return str(container.id), _read_port_map(container)
        except Exception:
            with _suppress_docker():
                container.remove(force=True)
            raise

    async def destroy_all(self) -> None:
        """Best-effort teardown of any leaked sidecars (labelled), on shutdown."""
        try:
            containers = await asyncio.to_thread(self._list_labelled)
        except DockerException:
            return
        for container in containers:
            with _suppress_docker():
                await asyncio.to_thread(container.remove, force=True)

    def _list_labelled(self) -> list[Any]:
        return list(self._get_client().containers.list(all=True, filters={"label": "titan.dind=1"}))


def _read_port_map(container: docker.models.containers.Container) -> dict[int, int]:
    """Map each pre-published inner port to the ephemeral host port Docker chose."""
    port_map: dict[int, int] = {}
    ports = (container.attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
    for spec, bindings in ports.items():
        if not bindings:
            continue
        inner = int(str(spec).split("/", 1)[0])
        host_port = next((int(b["HostPort"]) for b in bindings if b.get("HostPort")), None)
        if host_port is not None:
            port_map[inner] = host_port
    return port_map


def _safe_tar_name(image: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in image) + ".tar"


class _suppress_docker:
    def __enter__(self) -> "None":
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, DockerException)


__all__ = ["APP_PORTS", "DindController", "DindSidecar"]
