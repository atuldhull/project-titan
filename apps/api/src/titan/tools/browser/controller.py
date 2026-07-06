"""The Browser Controller — per-task headless-Chromium containers (Phase 22;
Vol 7 §5, TITAN-INT-032).

Chromium runs in its OWN container (``titan-browser`` running
``playwright run-server``), never in the Tool Host process. This controller
starts that container via the host daemon (the trusted Sandbox-Controller role),
publishes the Playwright server's websocket port to an ephemeral host port, and
waits until the server logs that it is listening. The :class:`BrowserSession`
then connects a Playwright client over that websocket and drives one page.

The container is non-privileged with the §5 CPU/mem ceiling (SEC-042); it needs
no workspace mount, no docker socket, and no credentials."""

import asyncio
import time
import uuid
from typing import Any

import docker
import structlog
from docker.errors import DockerException, ImageNotFound

from titan.tools.errors import ToolError
from titan.tools.sandbox.policy import SandboxPolicy

logger = structlog.get_logger("titan.tools.browser")

_SERVER_PORT = 3000
_READY_TIMEOUT_S = 40.0
# Chromium spawns a process tree (browser + zygote + renderers + GPU); 256 is too
# tight, so use a bounded-but-generous ceiling (still fork-bomb-contained).
_BROWSER_PIDS_LIMIT = 1024


class BrowserServer:
    """A live per-task browser container exposing a Playwright websocket."""

    def __init__(self, client: docker.DockerClient, container_id: str, *, ws_endpoint: str) -> None:
        self._client = client
        self._id = container_id
        self._ws_endpoint = ws_endpoint

    @property
    def container_id(self) -> str:
        return self._id

    @property
    def ws_endpoint(self) -> str:
        return self._ws_endpoint

    async def destroy(self) -> None:
        with _suppress_docker():
            await asyncio.to_thread(self._destroy_blocking)

    def _destroy_blocking(self) -> None:
        self._client.containers.get(self._id).remove(force=True)


class BrowserController:
    """Creates and destroys per-task browser containers. One per process."""

    def __init__(
        self,
        *,
        image: str,
        docker_host: str = "",
        policy: SandboxPolicy | None = None,
    ) -> None:
        self._image = image
        self._docker_host = docker_host
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

    async def create(self, task_id: uuid.UUID | None) -> BrowserServer:
        """Start the browser container, publish its ws port, and wait until the
        Playwright server is listening. Raises TOOL_UNAVAILABLE on failure."""
        try:
            container_id, host_port = await asyncio.to_thread(self._create_blocking, task_id)
        except ImageNotFound as exc:
            raise ToolError(
                "TOOL_UNAVAILABLE", f"browser image {self._image!r} is not available"
            ) from exc
        except DockerException as exc:
            raise ToolError("TOOL_UNAVAILABLE", f"browser daemon unavailable: {exc}") from exc
        server = BrowserServer(
            self._get_client(), container_id, ws_endpoint=f"ws://localhost:{host_port}/"
        )
        try:
            await self._wait_listening(container_id)
        except Exception:
            await server.destroy()
            raise
        return server

    def _create_blocking(self, task_id: uuid.UUID | None) -> tuple[str, int]:
        client = self._get_client()
        name = f"titan-browser-{task_id or uuid.uuid4()}"
        container = client.containers.run(
            image=self._image,
            name=name,
            detach=True,
            remove=False,
            ports={f"{_SERVER_PORT}/tcp": None},  # ephemeral host port
            mem_limit=self._policy.memory_bytes,
            nano_cpus=self._policy.nano_cpus,
            pids_limit=_BROWSER_PIDS_LIMIT,
            # This container renders attacker-controlled web content, so harden it
            # against a renderer escape (SEC-041 spirit): drop ALL capabilities and
            # forbid privilege escalation. Rootfs stays writable and the default
            # seccomp profile stays (Chromium needs /tmp + its unprivileged
            # user-namespace sandbox); gVisor is the stronger prod runtime (ADR-SEC-02).
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            labels={"titan.browser": "1", "titan.task": str(task_id or "adhoc")},
        )
        # A post-run failure must not orphan the container.
        try:
            container.reload()
            ports = (container.attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
            bindings = ports.get(f"{_SERVER_PORT}/tcp") or []
            host_port = next((int(b["HostPort"]) for b in bindings if b.get("HostPort")), None)
            if host_port is None:
                raise DockerException("browser server port was not published")
            return str(container.id), host_port
        except Exception:
            with _suppress_docker():
                container.remove(force=True)
            raise

    async def _wait_listening(self, container_id: str) -> None:
        # Wall-clock deadline (time.monotonic) so a slow log fetch can't stretch
        # the effective wait far past _READY_TIMEOUT_S.
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            logs = await asyncio.to_thread(self._logs_blocking, container_id)
            if "listening on ws" in logs.lower():
                return
            await asyncio.sleep(1.0)
        raise ToolError("TOOL_UNAVAILABLE", "the browser server did not become ready")

    def _logs_blocking(self, container_id: str) -> str:
        raw = self._get_client().containers.get(container_id).logs()
        return str(raw.decode("utf-8", "replace"))

    async def destroy_all(self) -> None:
        try:
            containers = await asyncio.to_thread(self._list_labelled)
        except DockerException:
            return
        for container in containers:
            with _suppress_docker():
                await asyncio.to_thread(container.remove, force=True)

    def _list_labelled(self) -> list[Any]:
        return list(
            self._get_client().containers.list(all=True, filters={"label": "titan.browser=1"})
        )


class _suppress_docker:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return isinstance(exc, DockerException)


__all__ = ["BrowserController", "BrowserServer"]
