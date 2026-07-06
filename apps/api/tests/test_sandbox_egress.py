"""Sandbox egress end-to-end (Phase 23; the DoD) — a REAL sandbox reaches the
internet ONLY through the audited forward proxy.

``sandbox`` (needs Docker + titan-sandbox) AND ``schema`` (the deny writes a
SANDBOX_VIOLATION audit row). The forward proxy runs in-process on the host; the
sandbox joins a bridge network and is pointed at it via ``host.docker.internal``
with the per-task credential injected by the SandboxController.

NOTE ON TOPOLOGY: full network-level deny-all (blocking a `--noproxy` direct
bypass) is enforced in compose/prod by the INTERNAL `sandbox-egress` network +
SEC-045 NetworkPolicy — Docker Desktop has no 'host-reachable but internet-denied'
mode for a host-run proxy, so this host test proves the proxy's deny+audit and
allowed-install behavior (the DoD claims) with the proxy env as the path."""

import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from titan.tools.egress.service import build_forward_proxy
from titan.tools.sandbox.controller import SandboxController
from titan.tools.sandbox.policy import EgressConfig, ResourceLimits, SandboxPolicy
from titan.tools.sandbox.workspace import WorkspaceManager

from ._container_tools import SEED_IMAGE  # titan-sandbox:local
from .conftest import run_async

pytestmark = [pytest.mark.sandbox, pytest.mark.schema]

_SECRET = "e2e-egress-secret"


def _docker_ready() -> bool:
    try:
        return bool(run_async(SandboxController(SandboxPolicy(image=SEED_IMAGE)).ping))
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_docker() -> None:
    if not _docker_ready():
        pytest.skip("no Docker / titan-sandbox:local for the egress e2e")


async def _audit_violations(engine: Any, project_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT count(*) FROM tool_audit "
                "WHERE project_id = :p AND error_code = 'SANDBOX_VIOLATION' AND tool = 'net.egress'"
            ),
            {"p": project_id},
        )
        return int(result.scalar_one())


def test_direct_egress_denied_and_audited_install_via_proxy_ok(migrated_engine: Any) -> None:
    async def scenario() -> None:
        # In-process forward proxy bound on all interfaces so the sandbox reaches
        # it via host.docker.internal.
        proxy = build_forward_proxy(migrated_engine, secret=_SECRET)
        server = await proxy.serve("0.0.0.0", 0)
        port = server.sockets[0].getsockname()[1]

        project_id, task_id = uuid.uuid4(), uuid.uuid4()
        egress = EgressConfig(
            network="bridge",
            proxy_host="host.docker.internal",
            proxy_port=port,
            secret=_SECRET,
        )
        controller = SandboxController(SandboxPolicy(image=SEED_IMAGE), egress=egress)
        root = Path(tempfile.mkdtemp(prefix="titan-egress-e2e-"))
        workspace = WorkspaceManager(root).for_call(project_id, task_id)
        sandbox = await controller.create(task_id, workspace, project_id=project_id)
        try:
            # (1) A direct egress to a non-allowlisted host FAILS (proxy denies)
            #     and is AUDITED as SANDBOX_VIOLATION.
            probe = await sandbox.exec(
                [
                    "python3",
                    "-c",
                    "import urllib.request as u; u.urlopen('https://example.com', timeout=8)",
                ],
                limits=ResourceLimits(timeout_s=30),
            )
            assert not probe.ok, f"egress to example.com should fail: {probe.stdout} {probe.stderr}"
            assert await _audit_violations(migrated_engine, project_id) >= 1

            # (2) pip install of a tiny pure-Python package SUCCEEDS via the proxy
            #     (pypi.org is allowlisted) — proving install only via the audited
            #     path. Needs internet; generous timeout.
            install = await sandbox.exec(
                [
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "--disable-pip-version-check",
                    "--target",
                    "/tmp/pt",
                    "six",
                ],
                limits=ResourceLimits(timeout_s=180),
            )
            assert install.ok, f"pip install via proxy should succeed: {install.stderr[-1500:]}"
            listed = await sandbox.exec(["ls", "/tmp/pt"])
            assert "six.py" in listed.stdout or "six" in listed.stdout
        finally:
            await sandbox.destroy()
            server.close()

    run_async(scenario)
