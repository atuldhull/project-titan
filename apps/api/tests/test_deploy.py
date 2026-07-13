"""The ``titan.deploy`` domain (Phase 48; Vol 7 §6.7 TITAN-INT-046, Vol 1
TITAN-FND-027).

Two layers, both Docker-free:

* pure unit tests of the orchestration logic (endpoint selection, probe retry);
* ``schema``-marked tests of the full deploy → health-check → record → teardown →
  re-deploy flow against the real test DB, with the Docker bring-up and the HTTP
  probe replaced by fakes — so ``deploy``/``teardown``/``re-deploy`` are integration-
  tested deterministically (the live-Docker DoD lives in ``test_tools_deploy``).
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import run_async
from titan.deploy import service as deploy
from titan.deploy.service import ComposeUpResult, ProbeResult

# --- pure orchestration units ------------------------------------------------


def test_select_endpoint_picks_the_lowest_published_port() -> None:
    chosen = deploy._select_endpoint(
        {"8080": "http://localhost:1", "3000": "http://localhost:2", "5000": "http://localhost:3"}
    )
    assert chosen == (3000, "http://localhost:2")


def test_select_endpoint_is_none_when_no_ports() -> None:
    assert deploy._select_endpoint({}) is None
    # A non-numeric key is ignored, not crashed on.
    assert deploy._select_endpoint({"tcp": "http://x"}) is None


def test_probe_retries_until_healthy() -> None:
    class FlakyProber:
        def __init__(self) -> None:
            self.calls = 0

        async def probe(self, url: str) -> ProbeResult:
            self.calls += 1
            ok = self.calls >= 3
            return ProbeResult(ok=ok, status_code=200 if ok else None, detail="")

    prober = FlakyProber()
    result = run_async(
        lambda: deploy._probe_until_healthy(prober, "http://x", attempts=5, delay_s=0.0)
    )
    assert result.ok and prober.calls == 3


def test_probe_gives_up_after_attempts() -> None:
    class DeadProber:
        def __init__(self) -> None:
            self.calls = 0

        async def probe(self, url: str) -> ProbeResult:
            self.calls += 1
            return ProbeResult(ok=False, status_code=None, detail="refused")

    prober = DeadProber()
    result = run_async(
        lambda: deploy._probe_until_healthy(prober, "http://x", attempts=3, delay_s=0.0)
    )
    assert not result.ok and prober.calls == 3


def test_deployment_model_maps_the_table() -> None:
    # Importing the model registers it on Base.metadata (and documents the schema).
    from titan.deploy.models import Deployment

    assert Deployment.__tablename__ == "deployments"
    cols = set(Deployment.__table__.columns.keys())
    assert {"project_id", "name", "compose_file", "status", "url", "port", "health"} <= cols
    assert {"created_at", "updated_at", "version"} <= cols


# --- fakes for the injected ports --------------------------------------------


class FakeDeployer:
    def __init__(
        self,
        endpoints: dict[str, str],
        *,
        services: tuple[str, ...] = ("web",),
        up_error: Exception | None = None,
    ) -> None:
        self._endpoints = endpoints
        self._services = services
        self._up_error = up_error
        self.up_calls = 0
        self.down_calls = 0
        self.logs_calls = 0

    async def up(self, compose_file: str, *, build: bool) -> ComposeUpResult:
        self.up_calls += 1
        if self._up_error is not None:
            raise self._up_error
        return ComposeUpResult(services=self._services, endpoints=dict(self._endpoints))

    async def down(self, compose_file: str) -> None:
        self.down_calls += 1

    async def stream_logs(self, compose_file: str) -> None:
        self.logs_calls += 1


class FakeProber:
    def __init__(self, *, ok: bool) -> None:
        self._ok = ok
        self.calls = 0

    async def probe(self, url: str) -> ProbeResult:
        self.calls += 1
        return ProbeResult(
            ok=self._ok,
            status_code=200 if self._ok else None,
            detail="HTTP 200" if self._ok else "refused",
        )


# --- schema-marked DB flow (Docker replaced by fakes) ------------------------


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
                "VALUES (:id, :n, :d, 'b', :u)"
            ),
            {"id": pid, "n": f"p-{pid.hex[:12]}", "d": "P", "u": uid},
        )
    return pid


def _deploy(
    engine: Any, project_id: uuid.UUID, deployer: Any, prober: Any, *, name: str = "demo"
) -> Any:
    return run_async(
        lambda: deploy.deploy_stack(
            engine,
            project_id=project_id,
            task_id=None,
            name=name,
            compose_file="docker-compose.yml",
            deployer=deployer,
            prober=prober,
            build=False,
            probe_attempts=2,
            probe_delay_s=0.0,
        )
    )


@pytest.mark.schema
def test_healthy_deploy_records_running_url_and_streams_logs(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    deployer = FakeDeployer({"8080": "http://localhost:34567"})
    prober = FakeProber(ok=True)
    result = _deploy(migrated_engine, pid, deployer, prober)

    assert result.status == "running"
    assert result.health == "green"
    assert result.url == "http://localhost:34567"
    assert result.port == 8080
    assert result.services == ["web"]
    # A healthy deploy pumps the container logs to the UI (best-effort).
    assert deployer.logs_calls == 1
    # And the record is persisted.
    fetched = run_async(lambda: _get(migrated_engine, result.id))
    assert fetched.status == "running" and fetched.url == "http://localhost:34567"


@pytest.mark.schema
def test_unhealthy_deploy_is_failed_and_url_cleared(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    deployer = FakeDeployer({"8080": "http://localhost:34567"})
    result = _deploy(migrated_engine, pid, deployer, FakeProber(ok=False))
    assert result.status == "failed"
    assert result.health == "red"
    assert result.url is None
    assert "health check failed" in result.detail
    # Never advertised as running, and no logs pumped for a dead app.
    assert deployer.logs_calls == 0


@pytest.mark.schema
def test_deploy_with_no_reachable_port_is_failed(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    result = _deploy(migrated_engine, pid, FakeDeployer({}), FakeProber(ok=True))
    assert result.status == "failed" and result.url is None
    assert "no host-reachable port" in result.detail


@pytest.mark.schema
def test_compose_up_failure_marks_failed_and_propagates(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    deployer = FakeDeployer({}, up_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        _deploy(migrated_engine, pid, deployer, FakeProber(ok=True))
    # The record still captures the failure (a fresh txn committed before re-raise).
    row = run_async(lambda: _get_by_name(migrated_engine, pid, "demo"))
    assert row.status == "failed" and "compose up failed" in row.detail


@pytest.mark.schema
def test_redeploy_is_idempotent_on_the_same_record(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    first = _deploy(
        migrated_engine, pid, FakeDeployer({"8080": "http://localhost:1"}), FakeProber(ok=True)
    )
    second = _deploy(
        migrated_engine, pid, FakeDeployer({"8080": "http://localhost:2"}), FakeProber(ok=True)
    )
    assert second.id == first.id  # same record, not a duplicate
    assert second.url == "http://localhost:2"  # re-deploy refreshed the URL
    # Exactly one deployment row for (project, name).
    count = run_async(lambda: _count(migrated_engine, pid, "demo"))
    assert count == 1


@pytest.mark.schema
def test_teardown_marks_stopped_and_brings_the_stack_down(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    _deploy(migrated_engine, pid, FakeDeployer({"8080": "http://localhost:1"}), FakeProber(ok=True))
    down_deployer = FakeDeployer({})
    result = run_async(
        lambda: deploy.teardown_stack(
            migrated_engine, project_id=pid, name="demo", deployer=down_deployer
        )
    )
    assert result is not None
    assert result.status == "stopped" and result.url is None
    assert down_deployer.down_calls == 1


@pytest.mark.schema
def test_teardown_of_unknown_app_is_a_noop(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    deployer = FakeDeployer({})
    result = run_async(
        lambda: deploy.teardown_stack(
            migrated_engine, project_id=pid, name="never-deployed", deployer=deployer
        )
    )
    assert result is None
    assert deployer.down_calls == 0  # nothing to tear down


@pytest.mark.schema
def test_reads_list_and_get(migrated_engine: Any) -> None:
    pid = run_async(lambda: _make_project(migrated_engine))
    _deploy(
        migrated_engine,
        pid,
        FakeDeployer({"8080": "http://localhost:1"}),
        FakeProber(ok=True),
        name="a",
    )
    _deploy(
        migrated_engine,
        pid,
        FakeDeployer({"8080": "http://localhost:2"}),
        FakeProber(ok=True),
        name="b",
    )

    async def reads() -> tuple[list[str], str]:
        async with migrated_engine.connect() as conn:
            listed = await deploy.list_deployments(conn, pid)
            one = await deploy.get_deployment(conn, listed[0].id)
            return sorted(d.name for d in listed), one.status

    names, status = run_async(reads)
    assert names == ["a", "b"] and status == "running"


# --- REST read endpoints (the Deployment view) -------------------------------


def _project_via_api(api: dict[str, Any]) -> uuid.UUID:
    name = f"proj-{uuid.uuid4().hex[:12]}"
    resp = api["client"].post(
        "/api/v1/projects",
        json={"name": name, "display_name": name, "brief": "b"},
        headers=api["token_for"]("owner"),
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(str(resp.json()["id"]))


@pytest.mark.schema
def test_rest_endpoints_list_and_get_a_deployment(api: dict[str, Any]) -> None:
    from titan.deploy import repository as repo

    pid = _project_via_api(api)

    async def seed() -> uuid.UUID:
        async with api["engine"].begin() as conn:
            row = await repo.upsert_pending(
                conn, pid, None, name="demo", compose_file="docker-compose.yml"
            )
            row = await repo.mark_result(
                conn,
                row.id,
                status="running",
                url="http://localhost:8080",
                port=8080,
                services=["web"],
                health="green",
                detail="",
            )
            return uuid.UUID(str(row.id))

    deployment_id = run_async(seed)
    client, headers = api["client"], api["token_for"]("owner")

    listed = client.get(f"/api/v1/projects/{pid}/deployments", headers=headers)
    assert listed.status_code == 200, listed.text
    assert any(
        d["name"] == "demo" and d["url"] == "http://localhost:8080" for d in listed.json()
    )

    one = client.get(f"/api/v1/deployments/{deployment_id}", headers=headers)
    assert one.status_code == 200 and one.json()["status"] == "running"

    missing = client.get(f"/api/v1/deployments/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404


# --- small DB read helpers ---------------------------------------------------


async def _get(engine: Any, deployment_id: uuid.UUID) -> Any:
    async with engine.connect() as conn:
        return await deploy.get_deployment(conn, deployment_id)


async def _get_by_name(engine: Any, project_id: uuid.UUID, name: str) -> Any:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT status, detail FROM deployments WHERE project_id = :p AND name = :n"),
            {"p": project_id, "n": name},
        )
        return row.one()


async def _count(engine: Any, project_id: uuid.UUID, name: str) -> int:
    async with engine.connect() as conn:
        row = await conn.execute(
            text("SELECT count(*) FROM deployments WHERE project_id = :p AND name = :n"),
            {"p": project_id, "n": name},
        )
        return int(row.scalar_one())
