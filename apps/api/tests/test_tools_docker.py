"""docker.* tests (Phase 22; Vol 7 §4.3 & §5, TITAN-INT-030, TITAN-SEC-042/044).

Two layers:

* Pure/DB tests (no Docker) — the SEC-044 socket-mount-probe is a pre-dispatch
  rejection, so it is proven through the bus with only the audit DB, plus the
  volume constraint + endpoint parser as units.
* ``sandbox``-marked tests (need Docker + docker:dind-rootless + titan-sandbox) —
  the DoD: ``compose_up`` of a fixture app is reachable from the test; a
  port-less ``docker.run`` gets deny-all egress.
"""

import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from titan.tools.builtin import docker as docker_tools

from ._container_tools import DindHarness, dind_ready
from .conftest import run_async

# --- pure unit tests (no Docker) ---------------------------------------------


def test_volume_constraint_blocks_socket_and_host_paths() -> None:
    block = docker_tools._volume_constraint
    assert block({"volumes": [{"source": "app", "target": "/app"}]}) is None
    for bad in ["/var/run/docker.sock", "/var/run/docker.sock", "../etc", "/etc/passwd", "/proc"]:
        verdict = block({"volumes": [{"source": bad, "target": "/x"}]})
        assert verdict is not None and verdict[0] == "SANDBOX_VIOLATION", bad
    # An absolute path outside /workspace is jailed even if not on the deny list.
    jailed = block({"volumes": [{"source": "/opt/data", "target": "/x"}]})
    assert jailed is not None and jailed[0] == "SANDBOX_VIOLATION"


def test_published_port_regex_extracts_dind_side_port() -> None:
    line = "0.0.0.0:8080->80/tcp, :::8080->80/tcp\n0.0.0.0:5000->5000/tcp"
    assert {int(m) for m in docker_tools._PUBLISHED.findall(line)} == {8080, 5000}


def test_build_paths_are_jailed_to_relative_forms() -> None:
    # The jail is REAL only if the resolved path is USED. An absolute in-sandbox
    # path must be remapped under the workspace (never passed raw to read the
    # sidecar's own /etc/hostname), and a traversal must be rejected.
    import tempfile
    from pathlib import Path
    from typing import cast

    from titan.tools.dind.session import DindSession
    from titan.tools.errors import ToolError
    from titan.tools.sandbox.workspace import WorkspaceManager

    ws = WorkspaceManager(Path(tempfile.mkdtemp(prefix="titan-jail-"))).for_call(
        uuid.uuid4(), uuid.uuid4()
    )
    session = DindSession(
        cast(Any, None), project_id=uuid.uuid4(), task_id=uuid.uuid4(), workspace=ws
    )
    assert docker_tools._jail_rel(session, ".") == "."
    assert docker_tools._jail_rel(session, "Dockerfile") == "Dockerfile"
    # Absolute in-sandbox path is remapped under the workspace, not passed raw.
    assert docker_tools._jail_rel(session, "/etc/hostname") == "etc/hostname"
    with pytest.raises(ToolError) as exc:
        docker_tools._jail_rel(session, "../../escape")
    assert exc.value.code == "SANDBOX_VIOLATION"


@pytest.mark.schema
def test_socket_mount_probe_is_blocked_and_audited(migrated_engine: Any) -> None:
    # DoD (host-socket half): a docker.run mounting the host docker socket is
    # rejected SANDBOX_VIOLATION *before dispatch* and lands one error audit row
    # — no sidecar is ever created.
    from sqlalchemy import text

    from titan.tools import repository as audit_repo
    from titan.tools.bus import ToolBus, ToolCaller, deny_all_approvals
    from titan.tools.envelopes import new_call
    from titan.tools.service import build_registry

    project_id = uuid.uuid4()

    async def clean() -> None:
        async with migrated_engine.begin() as conn:
            await conn.execute(text("TRUNCATE tool_audit"))

    run_async(clean)

    bus = ToolBus(build_registry(), migrated_engine, approval_gate=deny_all_approvals)
    caller = ToolCaller(
        agent_id="agent.backend.1",
        role="backend",
        project_id=project_id,
        granted_scopes=frozenset({"docker:run", "workspace:read", "workspace:write"}),
    )
    call = new_call(
        "docker.run",
        {
            "image": "alpine:3.20",
            "volumes": [{"source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}],
        },
        project_id=project_id,
        agent_id=caller.agent_id,
    )
    result = run_async(lambda: bus.invoke(call, caller))
    assert result.status == "error" and result.code == "SANDBOX_VIOLATION"

    async def rows() -> list[Any]:
        async with migrated_engine.connect() as conn:
            return await audit_repo.rows_for_call(conn, call.tool_call_id)

    audited = run_async(rows)
    assert [r.status for r in audited] == ["error"]  # audited, never dispatched


# --- sandbox-marked DoD tests (need Docker) ----------------------------------


@pytest.fixture(scope="module", autouse=True)
def _require_docker(request: pytest.FixtureRequest) -> None:
    # Only gate the sandbox-marked tests on Docker; the units above always run.
    if request.node.get_closest_marker("sandbox") and not dind_ready():
        pytest.skip("no Docker / docker:dind-rootless for the docker suite")


@pytest.fixture
def dind() -> Iterator[DindHarness]:
    h = DindHarness()
    try:
        yield h
    finally:
        h.close()


_COMPOSE = """services:
  web:
    build: .
    ports:
      - "8080:80"
"""
_DOCKERFILE = """FROM titan-sandbox:local
WORKDIR /srv
RUN echo "titan-compose-ok" > index.html
CMD ["python3", "-m", "http.server", "80"]
"""


def _http_get(url: str, *, attempts: int = 10) -> str:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 — localhost fixture
                return str(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last = exc
            time.sleep(0.6)
    raise AssertionError(f"fixture never became reachable at {url}: {last}")


@pytest.mark.sandbox
def test_compose_up_fixture_is_reachable_from_the_test(dind: DindHarness) -> None:
    # The DoD: compose_up of a fixture app makes it reachable from the test,
    # served by a container inside the per-task rootless DinD sidecar (built
    # FROM the auto-seeded titan-sandbox image — no registry egress).
    (dind.workspace.root / "docker-compose.yml").write_text(_COMPOSE)
    (dind.workspace.root / "Dockerfile").write_text(_DOCKERFILE)
    result = run_async(
        lambda: docker_tools._compose_up({"file": "docker-compose.yml", "build": True}, dind.ctx())
    ).result
    assert result["up"] is True
    assert result["services"], "compose reported no services"
    endpoints = result["endpoints"]
    assert "8080" in endpoints, f"no published endpoint discovered: {result}"
    body = _http_get(endpoints["8080"])
    assert "titan-compose-ok" in body
    # Teardown through the tool too (exercises compose_down).
    down = run_async(
        lambda: docker_tools._compose_down({"file": "docker-compose.yml"}, dind.ctx())
    ).result
    assert down["down"] is True


@pytest.mark.sandbox
def test_build_run_ps_logs_flow(dind: DindHarness) -> None:
    (dind.workspace.root / "Dockerfile").write_text(_DOCKERFILE)
    built = run_async(
        lambda: docker_tools._build({"tag": "fixture:local", "context": "."}, dind.ctx())
    ).result
    assert built["built"] is True and built["tag"] == "fixture:local"
    run = run_async(
        lambda: docker_tools._run(
            {"image": "fixture:local", "name": "fx", "ports": [{"published": 8081, "target": 80}]},
            dind.ctx(),
        )
    ).result
    assert len(run["container_id"]) >= 12
    assert "8081" in run["endpoints"]
    assert "titan-compose-ok" in _http_get(run["endpoints"]["8081"])
    listing = run_async(lambda: docker_tools._ps({}, dind.ctx())).result
    assert any(c["name"] == "fx" for c in listing["containers"])
    logs = run_async(lambda: docker_tools._logs({"container": "fx", "tail": 50}, dind.ctx())).result
    assert "exit_code" in logs


@pytest.mark.sandbox
def test_portless_run_gets_deny_all_egress(dind: DindHarness) -> None:
    # SEC-043: a container with no published ports is forced onto --network none,
    # so an egress attempt from inside it fails.
    run_async(
        lambda: docker_tools._run(
            {
                "image": "titan-sandbox:local",
                "name": "egress",
                "command": [
                    "sh",
                    "-c",
                    "wget -T 2 -q -O- http://example.com >/dev/null 2>&1 "
                    "&& echo EGRESS_OPEN || echo EGRESS_DENIED",
                ],
            },
            dind.ctx(),
        )
    )
    # The probe blocks on its own -T timeout before reporting; poll the logs.
    logs_text = ""
    for _ in range(15):
        logs_text = run_async(
            lambda: docker_tools._logs({"container": "egress", "tail": 20}, dind.ctx())
        ).result["logs"]
        if "EGRESS_" in logs_text:
            break
        time.sleep(1.0)
    assert "EGRESS_DENIED" in logs_text and "EGRESS_OPEN" not in logs_text


@pytest.mark.sandbox
def test_compose_workdir_escape_is_jailed(dind: DindHarness) -> None:
    from titan.tools.errors import ToolError

    with pytest.raises(ToolError) as exc:
        run_async(lambda: docker_tools._compose_up({"file": "../../etc/passwd"}, dind.ctx()))
    assert exc.value.code in {"SANDBOX_VIOLATION", "TOOL_UNAVAILABLE"}
