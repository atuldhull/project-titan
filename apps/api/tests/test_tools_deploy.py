"""``deploy.*`` tools + the local-deploy DoD (Phase 48; Vol 7 §6.7 TITAN-INT-046,
Vol 1 TITAN-FND-012/027).

Layers:

* pure unit tests (no Docker): the tools are registered, the HTTP prober classifies
  reachable/unreachable, and a missing sidecar fails closed;
* a ``schema``-marked bus test: ``deploy.compose`` is routable for DevOps and denied
  for other roles (the §4.3 allowlist);
* a ``sandbox``-marked live DoD: a self-contained generated app is brought up against
  a REAL rootless DinD sidecar with no cloud creds and no registry egress — its URL
  responds, the health probe passes, and its logs stream to the dashboard over
  tool.output. The deployment-record half of the flow (deploy/teardown/re-deploy
  idempotency + status/URL persistence) is proven against the DB in test_deploy; the
  two are split so neither needs both a nested docker daemon and heavy DB churn at
  once.
"""

import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from titan.tools.builtin import deploy as deploy_tools
from titan.tools.errors import ToolError
from titan.tools.registry import ToolExecutionContext
from titan.tools.service import build_registry
from titan.tools.streaming import ToolOutputStream

from ._container_tools import DindHarness, dind_ready
from .conftest import run_async

# --- pure unit tests (no Docker) ---------------------------------------------


def test_deploy_tools_are_registered_under_the_local_connector() -> None:
    registry = build_registry()
    compose = registry.get("deploy.compose")
    teardown = registry.get("deploy.teardown")
    assert compose is not None and teardown is not None
    for spec in (compose, teardown):
        assert spec.provider == "connector:titan.local"
        assert spec.risk == "write"
        assert spec.required_scopes == ("deploy:local",)


def test_require_dind_fails_closed_without_a_sidecar() -> None:
    ctx = ToolExecutionContext(
        engine=None,  # type: ignore[arg-type]
        project_id=uuid.uuid4(),
        task_id=None,
        agent_id="agent.devops.1",
        agent_uuid=None,
        dind=None,
    )
    with pytest.raises(ToolError) as exc:
        deploy_tools._require_dind(ctx)
    assert exc.value.code == "TOOL_UNAVAILABLE"


def test_http_prober_reports_unreachable_port() -> None:
    # Port 9 (discard) is virtually never listening on the loopback in CI.
    result = deploy_tools.HttpProber._probe_blocking("http://localhost:9/")
    assert result.ok is False and result.status_code is None


def test_http_prober_reports_ok_on_a_2xx() -> None:
    import http.server
    import threading

    class _Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args: Any) -> None:  # silence
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        result = deploy_tools.HttpProber._probe_blocking(f"http://127.0.0.1:{port}/")
        assert result.ok is True and result.status_code == 200
    finally:
        server.shutdown()


def test_endpoint_discovery_is_scoped_to_the_stacks_own_containers() -> None:
    """A reused per-task sidecar may run OTHER stacks' containers; endpoint
    discovery must advertise only THIS stack's published ports (by container id),
    or a deploy could record another app's URL (adversarial review finding)."""
    ps_output = (
        "aaaa1111||0.0.0.0:8080->80/tcp, :::8080->80/tcp\n"
        "bbbb2222||0.0.0.0:3000->3000/tcp\n"  # a DIFFERENT stack — must be excluded
        "cccc3333||\n"  # this stack, but no published port
    )
    stack = {"aaaa1111", "cccc3333"}
    assert deploy_tools._stack_published_ports(stack, ps_output) == {8080}
    # The lower-numbered 3000 belongs to another container and is NOT leaked in.
    assert 3000 not in deploy_tools._stack_published_ports(stack, ps_output)
    assert deploy_tools._stack_published_ports(set(), ps_output) == set()


# --- schema-marked bus authz test --------------------------------------------


@pytest.mark.schema
def test_deploy_compose_is_routable_for_devops_and_denied_for_others(
    migrated_engine: Any,
) -> None:
    from titan.tools.bus import ToolBus, ToolCaller, deny_all_approvals
    from titan.tools.envelopes import new_call

    project_id = uuid.uuid4()
    bus = ToolBus(build_registry(), migrated_engine, approval_gate=deny_all_approvals)
    args = {"name": "demo"}

    def _invoke(role: str, scopes: set[str]) -> Any:
        caller = ToolCaller(
            agent_id=f"agent.{role}.1",
            role=role,
            project_id=project_id,
            granted_scopes=frozenset(scopes),
        )
        call = new_call("deploy.compose", args, project_id=project_id, agent_id=caller.agent_id)
        return run_async(lambda: bus.invoke(call, caller))

    # DevOps is allow-listed for deploy.* and holds the scope → it passes authz and
    # only fails for want of a sidecar (proving it was NOT blocked by policy).
    devops = _invoke("devops", {"deploy:local", "workspace:write"})
    assert devops.status == "error" and devops.code == "TOOL_UNAVAILABLE"
    # Backend and CEO are denied deploy.* by the §4.3 allowlist.
    for role in ("backend", "ceo"):
        denied = _invoke(role, {"deploy:local", "workspace:write"})
        assert denied.status == "error" and denied.code == "PERMISSION_DENIED"


# --- sandbox-marked live DoD -------------------------------------------------


def _demo_app_files() -> dict[str, str]:
    """A self-contained generated app: it ships its own docker-compose.yml and
    builds + runs by itself FROM the offline base image (no registry egress)."""
    compose = 'services:\n  web:\n    build: .\n    ports:\n      - "8080:80"\n'
    dockerfile = (
        "FROM titan-sandbox:local\n"
        "WORKDIR /srv\n"
        'RUN echo "titan-deploy-ok" > index.html\n'
        'CMD ["python3", "-m", "http.server", "80"]\n'
    )
    main_py = '"""Demo backend."""\n\n\ndef health() -> dict[str, str]:\n    return {"ok": "1"}\n'
    return {
        "docker-compose.yml": compose,
        "Dockerfile": dockerfile,
        "app/main.py": main_py,
        "tests/test_smoke.py": "def test_health() -> None:\n    assert True\n",
        "docs/README.md": "# Demo\n\n## Quickstart\n\n```bash\ndocker compose up -d\n```\n",
        "deploy/run.sh": "#!/bin/sh\ndocker compose up -d\n",
        "config/app.toml": '[app]\nname = "demo"\n',
    }


class _RecordingRedis:
    """The minimal surface ToolOutputStream needs: an async publish that records."""

    def __init__(self, sink: list[tuple[str, str]]) -> None:
        self._sink = sink

    async def publish(self, channel: str, payload: str) -> None:
        self._sink.append((channel, payload))


def _http_get(url: str, *, attempts: int = 15) -> str:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — localhost fixture
                return str(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last = exc
            time.sleep(0.6)
    raise AssertionError(f"deployed app never became reachable at {url}: {last}")


@pytest.fixture
def dind() -> Iterator[DindHarness]:
    if not dind_ready():
        pytest.skip("no Docker / dind-rootless for the deploy DoD")
    h = DindHarness()
    try:
        yield h
    finally:
        h.close()


@pytest.mark.sandbox
def test_sidecar_deploys_reachable_app_and_streams_logs(dind: DindHarness) -> None:
    """The live-Docker DoD: the SidecarComposeDeployer brings a self-contained app up
    against a real rootless DinD sidecar; its URL responds, the health probe passes,
    and its logs stream to the dashboard over the tool.output channel — zero cloud
    creds, no registry egress."""
    for rel, content in _demo_app_files().items():
        path = dind.workspace.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    sidecar = run_async(lambda: dind.session.get())
    published: list[tuple[str, str]] = []
    stream = ToolOutputStream(
        _RecordingRedis(published),  # type: ignore[arg-type]
        project_id=dind.project_id,
        task_id=dind.task_id,
        agent_id="agent.devops.1",
        tool="deploy.compose",
        tool_call_id="tc_deploy",
    )
    deployer = deploy_tools.SidecarComposeDeployer(sidecar, dind.workspace, stream=stream)

    # Bring the stack up and discover its host-reachable endpoint — the URL responds.
    up = run_async(lambda: deployer.up("docker-compose.yml", build=True))
    assert up.services, "compose reported no services"
    assert up.endpoints, f"no reachable endpoint discovered: {up}"
    port = min(int(p) for p in up.endpoints)
    url = up.endpoints[str(port)]
    assert "titan-deploy-ok" in _http_get(url)

    # The health probe classifies it reachable (2xx/3xx)...
    probe = run_async(lambda: deploy_tools.HttpProber().probe(url))
    assert probe.ok and probe.status_code is not None and 200 <= probe.status_code < 400

    # ...and its logs stream to the dashboard over the tool.output channel.
    run_async(lambda: deployer.stream_logs("docker-compose.yml"))
    frames = [json.loads(payload) for _channel, payload in published]
    assert frames, "no tool.output frames were streamed"
    assert any(f.get("type") == "tool.output" for f in frames)
    assert all(chan == f"tool.output:{dind.project_id}" for chan, _ in published)

    # Teardown brings the stack down cleanly.
    run_async(lambda: deployer.down("docker-compose.yml"))
