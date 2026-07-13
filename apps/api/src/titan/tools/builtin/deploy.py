"""``deploy.*`` builtin tools — the ``titan.local`` connector (Vol 7 §6.7 table,
TITAN-INT-046; Vol 1 TITAN-FND-027).

``deploy.compose`` is the default, zero-credential deploy target: it brings a
generated app's own Compose stack up against the task's rootless DinD sidecar
(Phase 22, images pulled through the Phase-23 registry mirror), allocates a local
port, health-checks the reachable URL, records the deployment (``titan.deploy``),
and streams the deployment + container logs to the dashboard Logs view over the
existing ``tool.output`` channel. ``deploy.teardown`` brings the stack down and
marks it stopped. Both are idempotent on ``(project, name)``.

This module is the tool-layer *adapter*: it wires the DinD sidecar + live-output
stream into the ``ComposeDeployer``/``Prober`` ports the ``titan.deploy`` domain
orchestrates, so the domain stays free of any Docker import."""

import asyncio
import re
import urllib.error
import urllib.request
from typing import Any

from titan.deploy import service as deploy_service
from titan.deploy.service import ComposeUpResult, ProbeResult
from titan.tools.dind.controller import DindSidecar
from titan.tools.dind.session import DindSession
from titan.tools.errors import ToolError
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolSpec
from titan.tools.sandbox.workspace import Workspace
from titan.tools.streaming import ToolOutputStream

_WORKSPACE_MOUNT = "/workspace"
_PUBLISHED = re.compile(r":(\d+)->\d+/tcp")
_PROVIDER = "connector:titan.local"


def _require_dind(ctx: ToolExecutionContext) -> DindSession:
    if ctx.dind is None:
        raise ToolError("TOOL_UNAVAILABLE", "no DinD sidecar is available for this task")
    return ctx.dind


def _stack_published_ports(stack_ids: set[str], ps_output: str) -> set[int]:
    """The published (sidecar-side) ports of ONLY the given stack's containers,
    from a ``docker ps --no-trunc --format {{.ID}}||{{.Ports}}`` listing. Ports of
    containers outside the stack are excluded — so a reused per-task sidecar can
    never leak another app's port into this deploy's endpoints."""
    ports: set[int] = set()
    for line in ps_output.splitlines():
        cid, _, port_text = line.partition("||")
        if cid.strip() not in stack_ids:
            continue
        ports.update(int(m) for m in _PUBLISHED.findall(port_text))
    return ports


class SidecarComposeDeployer:
    """A :class:`~titan.deploy.service.ComposeDeployer` over a DinD sidecar. Each
    compose invocation streams its output to the dashboard when a live-output
    stream is present (the Logs-view path)."""

    def __init__(
        self,
        sidecar: DindSidecar,
        workspace: Workspace,
        *,
        stream: ToolOutputStream | None,
    ) -> None:
        self._sidecar = sidecar
        self._workspace = workspace
        self._sink = stream.emit if stream is not None else None

    def _resolve(self, compose_file: str) -> tuple[str, str]:
        """Jail the compose path to the workspace and return (workdir, filename)
        for an in-sidecar ``docker compose -f`` invocation."""
        resolved = self._workspace.resolve(compose_file, must_exist=True)
        rel = self._workspace.relative(resolved)
        workdir_rel, _, filename = rel.rpartition("/")
        workdir = f"{_WORKSPACE_MOUNT}/{workdir_rel}" if workdir_rel else _WORKSPACE_MOUNT
        return workdir, filename

    async def _discover_endpoints(self, workdir: str, filename: str) -> dict[str, str]:
        """Map THIS stack's host-reachable published ports to their URLs.

        Scoped to the compose stack, not a global ``docker ps``: the per-task DinD
        sidecar is pooled and reused by every docker.*/deploy.* call in the task,
        so a global port scan could surface (and advertise) a *different* stack's
        container. We take only the containers ``compose ps`` reports for this
        file, then read their published ports from the daemon."""
        ids_result = await self._sidecar.exec_docker(
            ["compose", "-f", filename, "ps", "-q"], workdir=workdir, timeout_s=20
        )
        stack_ids = {line.strip() for line in ids_result.stdout.splitlines() if line.strip()}
        if not stack_ids:
            return {}
        listing = await self._sidecar.exec_docker(
            ["ps", "--no-trunc", "--format", "{{.ID}}||{{.Ports}}"], timeout_s=20
        )
        endpoints: dict[str, str] = {}
        for published in _stack_published_ports(stack_ids, listing.stdout):
            url = self._sidecar.endpoint(published)
            if url is not None:
                endpoints[str(published)] = url
        return endpoints

    async def up(self, compose_file: str, *, build: bool) -> ComposeUpResult:
        workdir, filename = self._resolve(compose_file)
        argv = ["compose", "-f", filename, "up", "-d"]
        if build:
            argv.append("--build")
        result = await self._sidecar.exec_docker(
            argv, workdir=workdir, timeout_s=900, on_chunk=self._sink
        )
        if not result.ok:
            raise ToolError("UPSTREAM_ERROR", f"compose up failed: {result.stderr.strip()[-2000:]}")
        services = await self._sidecar.exec_docker(
            ["compose", "-f", filename, "ps", "--format", "{{.Name}}"],
            workdir=workdir,
            timeout_s=30,
        )
        return ComposeUpResult(
            services=tuple(s for s in services.stdout.splitlines() if s.strip()),
            endpoints=await self._discover_endpoints(workdir, filename),
        )

    async def down(self, compose_file: str) -> None:
        workdir, filename = self._resolve(compose_file)
        result = await self._sidecar.exec_docker(
            ["compose", "-f", filename, "down"],
            workdir=workdir,
            timeout_s=120,
            on_chunk=self._sink,
        )
        if not result.ok:
            tail = result.stderr.strip()[-2000:]
            raise ToolError("UPSTREAM_ERROR", f"compose down failed: {tail}")

    async def stream_logs(self, compose_file: str) -> None:
        # Nothing to pump when no stream is wired.
        if self._sink is None:
            return
        workdir, filename = self._resolve(compose_file)
        await self._sidecar.exec_docker(
            ["compose", "-f", filename, "logs", "--tail", "200", "--no-color"],
            workdir=workdir,
            timeout_s=30,
            on_chunk=self._sink,
        )


class HttpProber:
    """A :class:`~titan.deploy.service.Prober` that GETs the candidate URL. The
    synchronous urllib call runs in a thread so the async bus is never blocked."""

    async def probe(self, url: str) -> ProbeResult:
        return await asyncio.to_thread(self._probe_blocking, url)

    @staticmethod
    def _probe_blocking(url: str) -> ProbeResult:
        try:
            request = urllib.request.Request(url, method="GET")  # noqa: S310 — localhost deploy URL
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                code = int(response.status)
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            return ProbeResult(ok=False, status_code=None, detail=str(exc)[:200])
        return ProbeResult(ok=200 <= code < 400, status_code=code, detail=f"HTTP {code}")


# --- executors ---------------------------------------------------------------


async def _compose(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_dind(ctx)
    sidecar = await session.get()
    deployer = SidecarComposeDeployer(sidecar, session.workspace, stream=session.stream)
    deployment = await deploy_service.deploy_stack(
        ctx.engine,
        project_id=ctx.project_id,
        task_id=ctx.task_id,
        name=str(args["name"]),
        compose_file=str(args.get("file", "docker-compose.yml")),
        deployer=deployer,
        prober=HttpProber(),
        build=bool(args.get("build", True)),
    )
    return ExecutionOutcome(
        result={
            "deployment_id": str(deployment.id),
            "name": deployment.name,
            "status": deployment.status,
            "url": deployment.url,
            "port": deployment.port,
            "services": deployment.services,
            "healthy": deployment.health == "green",
        }
    )


async def _teardown(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_dind(ctx)
    sidecar = await session.get()
    deployer = SidecarComposeDeployer(sidecar, session.workspace, stream=session.stream)
    deployment = await deploy_service.teardown_stack(
        ctx.engine, project_id=ctx.project_id, name=str(args["name"]), deployer=deployer
    )
    if deployment is None:
        return ExecutionOutcome(result={"torn_down": False, "status": "absent"})
    return ExecutionOutcome(result={"torn_down": True, "status": deployment.status})


# --- specs -------------------------------------------------------------------

_OBJ = {"type": "object"}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_NAME = {"type": "string", "minLength": 1, "maxLength": 128}
_FILE = {"type": "string", "maxLength": 4096}


SPECS = (
    ToolSpec(
        name="deploy.compose",
        provider=_PROVIDER,
        description=(
            "Deploy a generated app locally: bring its Compose stack up, health-check "
            "its URL, record the deployment, and stream logs to the dashboard."
        ),
        risk="write",
        required_scopes=("deploy:local",),
        input_schema=_obj({"name": _NAME, "file": _FILE, "build": {"type": "boolean"}}, ["name"]),
        output_schema=_OBJ,
        executor=_compose,
        timeout_s=960.0,  # the in-sidecar compose exec timeout is the real bound
        rate_limit_per_min=30,
    ),
    ToolSpec(
        name="deploy.teardown",
        provider=_PROVIDER,
        description="Tear down a locally deployed app's Compose stack and mark it stopped.",
        risk="write",
        required_scopes=("deploy:local",),
        input_schema=_obj({"name": _NAME}, ["name"]),
        output_schema=_OBJ,
        executor=_teardown,
        timeout_s=180.0,
        rate_limit_per_min=30,
    ),
)

__all__ = ["HttpProber", "SidecarComposeDeployer", "SPECS"]
