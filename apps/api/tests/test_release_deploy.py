"""The G10 (H10) release-deploy executor (Phase 49; Vol 6 §2.3 H10, TITAN-WFL-004/005).

Two layers:

* schema-marked hermetic tests (Docker replaced by fakes): the healthy path returns
  a green outcome; an unhealthy deploy runs the G10 failure path — rollback, a
  ``deploy.incident`` outbox event, and a defect task that re-enters engineering (H6).
* a ``sandbox``-marked live DoD: an app that comes up but never serves HTTP is
  deployed against a REAL rootless DinD sidecar; its health check goes red and the
  executor rolls it back, emits the incident, and opens the H6 defect task — no stubs.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.deploy.service import ComposeUpResult, ProbeResult
from titan.handoffs import service as handoffs

# --- fakes for the injected ports --------------------------------------------


class FakeDeployer:
    def __init__(self, endpoints: dict[str, str]) -> None:
        self._endpoints = endpoints
        self.down_calls = 0

    async def up(self, compose_file: str, *, build: bool) -> ComposeUpResult:
        return ComposeUpResult(services=("web",), endpoints=dict(self._endpoints))

    async def down(self, compose_file: str) -> None:
        self.down_calls += 1

    async def stream_logs(self, compose_file: str) -> None:
        return None


class FakeProber:
    def __init__(self, *, ok: bool) -> None:
        self._ok = ok

    async def probe(self, url: str) -> ProbeResult:
        return ProbeResult(ok=self._ok, status_code=200 if self._ok else None, detail="")


async def _make_project(engine: Any) -> uuid.UUID:
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :e, 'creator', 'local', :s)"
            ),
            {"id": uid, "e": f"u-{uid.hex[:12]}@t.local", "s": str(uid)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :n, 'P', 'b', :u)"
            ),
            {"id": pid, "n": f"p-{pid.hex[:12]}", "u": uid},
        )
    return pid


def _run_gate(engine: Any, pid: uuid.UUID, deployer: Any, prober: Any) -> Any:
    return run_async(
        lambda: handoffs.run_deploy_gate(
            engine,
            project_id=pid,
            deploy_name="demo",
            compose_file="docker-compose.yml",
            deployer=deployer,
            prober=prober,
            actor="agent.devops.1",
            soak_attempts=2,
            soak_delay_s=0.0,
            build=False,
        )
    )


async def _incident_events(engine: Any) -> int:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT count(*) FROM outbox WHERE payload->'data'->>'type' = 'deploy.incident'")
        )
        return int(row.scalar_one())


async def _latest_incident_health(engine: Any) -> tuple[str, str]:
    """The (health, detail) the newest deploy.incident event carries — it must
    preserve the FAILED record's red health + reason, not the rolled-back blank."""
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT payload->'data'->'deployment'->>'health', "
                "payload->'data'->'deployment'->>'detail' FROM outbox "
                "WHERE payload->'data'->>'type' = 'deploy.incident' ORDER BY id DESC LIMIT 1"
            )
        )
        health, detail = row.one()
        return str(health), str(detail)


@pytest.mark.schema
def test_healthy_deploy_returns_green_and_does_not_roll_back(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    deployer = FakeDeployer({"8080": "http://localhost:1"})
    outcome = _run_gate(migrated_engine, pid, deployer, FakeProber(ok=True))
    assert outcome.healthy is True
    assert outcome.rolled_back is False
    assert outcome.defect_task_id is None
    assert deployer.down_calls == 0


@pytest.mark.schema
def test_unhealthy_deploy_rolls_back_emits_incident_and_opens_h6_defect(
    migrated_engine: Any,
) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    incidents_before = run_async(lambda: _incident_events(migrated_engine))
    deployer = FakeDeployer({"8080": "http://localhost:1"})
    outcome = _run_gate(migrated_engine, pid, deployer, FakeProber(ok=False))

    assert outcome.healthy is False
    assert outcome.rolled_back is True and deployer.down_calls == 1  # rollback
    assert outcome.incident_emitted is True
    assert outcome.defect_task_id is not None
    # The deployment record is stopped (its URL no longer advertised).
    assert outcome.deployment.status == "stopped" and outcome.deployment.url is None
    # An incident event was emitted to the outbox...
    assert run_async(lambda: _incident_events(migrated_engine)) == incidents_before + 1
    # ...carrying the FAILED record's red health + failure reason (not the blank
    # post-rollback record) so a consumer can diagnose why it failed.
    health, detail = run_async(lambda: _latest_incident_health(migrated_engine))
    assert health == "red" and "health check failed" in detail

    # ...and a defect task was opened (the H6 re-entry).
    async def defect_exists() -> bool:
        async with migrated_engine.connect() as conn:
            row = await conn.execute(
                text("SELECT project_id FROM tasks WHERE id = :id"),
                {"id": outcome.defect_task_id},
            )
            found = row.one_or_none()
            return found is not None and found.project_id == pid

    assert run_async(defect_exists) is True


# --- sandbox-marked live DoD -------------------------------------------------

_FAILING_COMPOSE = 'services:\n  web:\n    build: .\n    ports:\n      - "8080:80"\n'
# Comes up cleanly (compose up succeeds) but never serves HTTP → health goes red.
_FAILING_DOCKERFILE = 'FROM titan-sandbox:local\nCMD ["sleep", "3600"]\n'


@pytest.fixture
def dind() -> Iterator[Any]:
    from ._container_tools import DindHarness, dind_ready

    if not dind_ready():
        pytest.skip("no Docker / dind-rootless for the live G10 DoD")
    h = DindHarness()
    try:
        yield h
    finally:
        h.close()


@pytest.mark.sandbox
def test_live_g10_failure_rolls_back_and_reopens_at_h6(dind: Any, migrated_engine: Any) -> None:
    from titan.tools.builtin.deploy import HttpProber, SidecarComposeDeployer

    (dind.workspace.root / "docker-compose.yml").write_text(_FAILING_COMPOSE)
    (dind.workspace.root / "Dockerfile").write_text(_FAILING_DOCKERFILE)
    pid = run_async(lambda: _make_project(migrated_engine))
    sidecar = run_async(lambda: dind.session.get())
    deployer = SidecarComposeDeployer(sidecar, dind.workspace, stream=None)
    incidents_before = run_async(lambda: _incident_events(migrated_engine))

    outcome = run_async(
        lambda: handoffs.run_deploy_gate(
            migrated_engine,
            project_id=pid,
            deploy_name="demo",
            compose_file="docker-compose.yml",
            deployer=deployer,
            prober=HttpProber(),
            actor="agent.devops.1",
            soak_attempts=3,
            soak_delay_s=1.0,
            build=True,
        )
    )
    # The real deploy came up but never served HTTP → health red → the G10 failure path.
    assert outcome.healthy is False
    assert outcome.rolled_back is True
    assert outcome.incident_emitted is True
    assert outcome.defect_task_id is not None
    assert outcome.deployment.status == "stopped"
    assert run_async(lambda: _incident_events(migrated_engine)) == incidents_before + 1
