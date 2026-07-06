"""``docker.*`` builtin tools (provider ``builtin:docker``) — container builds
and runs against a per-task ROOTLESS DinD sidecar (Vol 7 §4.3 & §5, TITAN-INT-030,
TITAN-SEC-042/044).

Every tool execs the docker CLI *inside* the task's DinD sidecar against its own
rootless daemon — the HOST daemon socket is never mounted or reachable (SEC-044).
A service the sidecar starts is host-reachable only through the fixed ports the
controller pre-published (``APP_PORTS``); the tools report the resulting host
URLs. Bind mounts are jailed to the workspace and may never reference the docker
socket or host-sensitive paths (a mount probe is rejected SANDBOX_VIOLATION and
audited).

Egress (SEC-043): a port-less ``docker.run`` is forced onto ``--network none``
(demonstrable deny-all). A published-port service, ``docker.build`` RUN step, or
compose stack needs a routable network, so its full deny-all-egress-except-the-
registry-mirror is enforced OUTSIDE the container network — by the authenticating
forward proxy + registry mirror (Phase 23, the mechanism SEC-043 itself
prescribes) and, in production, the SEC-045 default-deny NetworkPolicy. This
mirrors ADR-SEC-02: local dev accepts container-grade isolation, compensated by
egress-deny enforced at the proxy."""

import json
import re
from typing import Any

from titan.tools.dind.controller import APP_PORTS, DindSidecar
from titan.tools.dind.session import DindSession
from titan.tools.errors import ToolError, ToolErrorCode
from titan.tools.registry import ExecutionOutcome, ToolExecutionContext, ToolSpec

_WORKSPACE_MOUNT = "/workspace"
# Host paths a workspace container may never bind — the docker socket and the
# sidecar's own sensitive trees (SEC-044). Sources must be workspace-relative.
_FORBIDDEN_MOUNT_TOKENS = ("docker.sock", "..")
_FORBIDDEN_MOUNT_PREFIXES = ("/var/run", "/run/", "/proc", "/sys", "/etc", "/root", "/home", "/dev")
_PUBLISHED = re.compile(r":(\d+)->\d+/tcp")


def _require_dind(ctx: ToolExecutionContext) -> DindSession:
    if ctx.dind is None:
        raise ToolError("TOOL_UNAVAILABLE", "no DinD sidecar is available for this task")
    return ctx.dind


def _jail_rel(session: DindSession, path: str) -> str:
    """Resolve ``path`` inside the workspace jail (SANDBOX_VIOLATION on escape) and
    return its /workspace-relative form (``.``, ``sub``, ``sub/Dockerfile``) — the
    value that MUST be handed to docker under workdir=/workspace so the jail is
    actually enforced."""
    return session.workspace.relative(session.workspace.resolve(path))


def _volume_constraint(args: dict[str, Any]) -> "tuple[ToolErrorCode, str] | None":
    """TITAN-SEC-044: reject any bind mount that could reach the docker socket or
    a host-sensitive path. Sources must be workspace-relative — never absolute
    (except explicitly under /workspace) and never traversing out."""
    for entry in args.get("volumes") or []:
        source = str(entry.get("source", "")) if isinstance(entry, dict) else str(entry)
        low = source.lower()
        if any(tok in low for tok in _FORBIDDEN_MOUNT_TOKENS):
            return ("SANDBOX_VIOLATION", f"at '/volumes': mount source {source!r} is not permitted")
        if source.startswith("/") and not source.startswith(_WORKSPACE_MOUNT):
            return ("SANDBOX_VIOLATION", f"at '/volumes': absolute host mount {source!r} is jailed")
        if any(low.startswith(pfx) for pfx in _FORBIDDEN_MOUNT_PREFIXES):
            return ("SANDBOX_VIOLATION", f"at '/volumes': mount source {source!r} is not permitted")
    return None


async def _discover_endpoints(sidecar: DindSidecar) -> dict[str, str]:
    """Map each running container's host-reachable published port to its URL."""
    listing = await sidecar.exec_docker(["ps", "--format", "{{.Ports}}"], timeout_s=20)
    endpoints: dict[str, str] = {}
    for published in {int(m) for m in _PUBLISHED.findall(listing.stdout)}:
        url = sidecar.endpoint(published)
        if url is not None:
            endpoints[str(published)] = url
    return endpoints


# --- executors ---------------------------------------------------------------


async def _build(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_dind(ctx)
    sidecar = await session.get()
    # Jail BOTH paths and pass the resolved workspace-relative forms to docker —
    # NOT the raw model strings. Passing the raw string would let an absolute
    # "/etc/hostname" read the sidecar's own file (the resolve() return value must
    # be used, or the jail is a no-op).
    context = _jail_rel(session, str(args.get("context", ".")))
    dockerfile = _jail_rel(session, str(args.get("dockerfile", "Dockerfile")))
    argv = ["build", "-t", str(args["tag"]), "-f", dockerfile, context]
    result = await sidecar.exec_docker(argv, timeout_s=600)
    return ExecutionOutcome(
        result={
            "tag": str(args["tag"]),
            "built": result.ok,
            "exit_code": result.exit_code,
            "stdout_tail": result.stdout[-4000:],
        },
        stderr_tail=result.stderr,
    )


async def _run(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_dind(ctx)
    sidecar = await session.get()
    argv = ["run", "-d"]
    if args.get("name"):
        argv += ["--name", str(args["name"])]
    ports = args.get("ports") or []
    for entry in ports:
        argv += ["-p", f"{int(entry['published'])}:{int(entry['target'])}"]
    # Deny-all egress when nothing is published; a published service needs the
    # bridge (an `internal` network would break the host port-forward chain).
    if not ports:
        argv += ["--network", "none"]
    for entry in args.get("volumes") or []:
        rel = _jail_rel(session, str(entry["source"]))  # SANDBOX_VIOLATION on escape
        target = str(entry["target"])
        mount = f"{_WORKSPACE_MOUNT}/{rel}" if rel != "." else _WORKSPACE_MOUNT
        argv += ["-v", f"{mount}:{target}" + (":ro" if entry.get("read_only") else "")]
    for key, value in (args.get("env") or {}).items():
        argv += ["-e", f"{key}={value}"]
    argv.append(str(args["image"]))
    argv += [str(c) for c in (args.get("command") or [])]
    result = await sidecar.exec_docker(argv, timeout_s=120)
    if not result.ok:
        raise ToolError("UPSTREAM_ERROR", f"docker run failed: {result.stderr.strip()}")
    return ExecutionOutcome(
        result={
            "container_id": result.stdout.strip().splitlines()[-1][:64],
            "endpoints": await _discover_endpoints(sidecar),
        }
    )


async def _compose_up(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_dind(ctx)
    sidecar = await session.get()
    file = str(args.get("file", "docker-compose.yml"))
    resolved = session.workspace.resolve(file, must_exist=True)  # jail + must exist
    rel = session.workspace.relative(resolved)
    workdir_rel, _, filename = rel.rpartition("/")
    workdir = f"{_WORKSPACE_MOUNT}/{workdir_rel}" if workdir_rel else _WORKSPACE_MOUNT
    argv = ["compose", "-f", filename, "up", "-d"]
    if args.get("build"):
        argv.append("--build")
    result = await sidecar.exec_docker(argv, workdir=workdir, timeout_s=900)
    if not result.ok:
        raise ToolError("UPSTREAM_ERROR", f"compose up failed: {result.stderr.strip()[-2000:]}")
    services = await sidecar.exec_docker(
        ["compose", "-f", filename, "ps", "--format", "{{.Name}}"], workdir=workdir, timeout_s=30
    )
    return ExecutionOutcome(
        result={
            "up": True,
            "services": [s for s in services.stdout.splitlines() if s.strip()],
            "endpoints": await _discover_endpoints(sidecar),
        },
        stderr_tail=result.stderr,
    )


async def _compose_down(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_dind(ctx)
    sidecar = await session.get()
    file = str(args.get("file", "docker-compose.yml"))
    resolved = session.workspace.resolve(file, must_exist=True)
    rel = session.workspace.relative(resolved)
    workdir_rel, _, filename = rel.rpartition("/")
    workdir = f"{_WORKSPACE_MOUNT}/{workdir_rel}" if workdir_rel else _WORKSPACE_MOUNT
    argv = ["compose", "-f", filename, "down"]
    if args.get("volumes"):
        argv.append("-v")
    result = await sidecar.exec_docker(argv, workdir=workdir, timeout_s=120)
    return ExecutionOutcome(
        result={"down": result.ok, "exit_code": result.exit_code}, stderr_tail=result.stderr
    )


async def _logs(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_dind(ctx)
    sidecar = await session.get()
    tail = min(int(args.get("tail", 200)), 5000)
    result = await sidecar.exec_docker(
        ["logs", "--tail", str(tail), str(args["container"])], timeout_s=30
    )
    return ExecutionOutcome(
        result={"logs": (result.stdout + result.stderr)[-16000:], "exit_code": result.exit_code}
    )


async def _ps(args: dict[str, Any], ctx: ToolExecutionContext) -> ExecutionOutcome:
    session = _require_dind(ctx)
    sidecar = await session.get()
    argv = ["ps", "--format", "json"]
    if args.get("all"):
        argv.insert(1, "-a")
    result = await sidecar.exec_docker(argv, timeout_s=30)
    containers: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(
            {
                "id": str(row.get("ID", ""))[:64],
                "image": row.get("Image", ""),
                "name": row.get("Names", ""),
                "status": row.get("Status", ""),
                "ports": row.get("Ports", ""),
            }
        )
    return ExecutionOutcome(result={"containers": containers})


# --- specs -------------------------------------------------------------------

_OBJ = {"type": "object"}
_ENV = {"type": "object", "additionalProperties": {"type": "string"}}
_VOLUMES = {
    "type": "array",
    "maxItems": 32,
    "items": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "maxLength": 4096},
            "target": {"type": "string", "maxLength": 4096},
            "read_only": {"type": "boolean"},
        },
        "required": ["source", "target"],
        "additionalProperties": False,
    },
}
_PORTS = {
    "type": "array",
    "maxItems": 16,
    "items": {
        "type": "object",
        "properties": {
            "published": {"type": "integer", "enum": list(APP_PORTS)},
            "target": {"type": "integer", "minimum": 1, "maximum": 65535},
        },
        "required": ["published", "target"],
        "additionalProperties": False,
    },
}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _spec(
    name: str,
    description: str,
    executor: Any,
    schema: dict[str, Any],
    *,
    constraints: tuple[Any, ...] = (),
) -> ToolSpec:
    return ToolSpec(
        name=name,
        provider="builtin:docker",
        description=description,
        risk="write",
        required_scopes=("docker:run",),
        input_schema=schema,
        output_schema=_OBJ,
        executor=executor,
        timeout_s=960.0,  # the in-sidecar exec timeout is the real bound
        rate_limit_per_min=60,
        constraints=constraints,
    )


SPECS = (
    _spec(
        "docker.build",
        "Build an image in the task's DinD sidecar from a workspace Dockerfile.",
        _build,
        _obj(
            {
                "tag": {"type": "string", "minLength": 1, "maxLength": 255},
                "context": {"type": "string", "maxLength": 4096},
                "dockerfile": {"type": "string", "maxLength": 4096},
            },
            ["tag"],
        ),
    ),
    _spec(
        "docker.run",
        "Run a container in the DinD sidecar (deny-all egress unless a port is published).",
        _run,
        _obj(
            {
                "image": {"type": "string", "minLength": 1, "maxLength": 255},
                "name": {"type": "string", "maxLength": 128},
                "command": {"type": "array", "items": {"type": "string"}, "maxItems": 128},
                "ports": _PORTS,
                "env": _ENV,
                "volumes": _VOLUMES,
            },
            ["image"],
        ),
        constraints=(_volume_constraint,),
    ),
    _spec(
        "docker.compose_up",
        "Bring up a compose stack in the DinD sidecar; reports host-reachable endpoints.",
        _compose_up,
        _obj(
            {
                "file": {"type": "string", "maxLength": 4096},
                "build": {"type": "boolean"},
            },
            [],
        ),
    ),
    _spec(
        "docker.compose_down",
        "Tear down a compose stack in the DinD sidecar.",
        _compose_down,
        _obj({"file": {"type": "string", "maxLength": 4096}, "volumes": {"type": "boolean"}}, []),
    ),
    _spec(
        "docker.logs",
        "Fetch a container's logs from the DinD sidecar.",
        _logs,
        _obj(
            {
                "container": {"type": "string", "minLength": 1, "maxLength": 255},
                "tail": {"type": "integer", "minimum": 1, "maximum": 5000},
            },
            ["container"],
        ),
    ),
    _spec(
        "docker.ps",
        "List containers in the DinD sidecar.",
        _ps,
        _obj({"all": {"type": "boolean"}}, []),
    ),
)

__all__ = ["SPECS"]
