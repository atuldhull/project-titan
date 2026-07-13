"""Public service interface of the ``deploy`` module (Vol 7 §6.7, TITAN-INT-046;
Vol 1 TITAN-FND-027).

``deploy_stack`` brings a generated app's Compose stack up, health-checks its
reachable URL, and records the deployment; ``teardown_stack`` tears it down and
marks it stopped; both are idempotent on ``(project_id, name)``. The Docker bring-
up and the HTTP health-probe are done through injected ``ComposeDeployer`` /
``Prober`` ports — the tool layer supplies real ones (a DinD sidecar that streams
live logs to the dashboard, a urllib probe), tests supply fakes — so this module
never imports ``titan.tools`` and the whole flow is hermetically testable.

Bring-up and probing run OUTSIDE any DB transaction (a slow build or an
unreachable app must never hold a database lock); each state change is then
persisted in its own short transaction. Only this interface crosses the module
boundary; the models, repository, and router stay private. Pydantic v2 DTOs
cross the boundary."""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.deploy import repository as repo
from titan.gateway.rbac import not_found

# Health-probe budget: ~15s of retries at 0.75s spacing, enough for a freshly
# built container to bind its port. Callers (tests) may shrink both.
_DEFAULT_PROBE_ATTEMPTS = 20
_DEFAULT_PROBE_DELAY_S = 0.75


# --- injected ports ----------------------------------------------------------


@dataclass(frozen=True)
class ComposeUpResult:
    """What a ``ComposeDeployer.up`` reports: the service names it started and a
    map of each host-reachable published port (as a string) to its URL."""

    services: tuple[str, ...]
    endpoints: dict[str, str]


@dataclass(frozen=True)
class ProbeResult:
    """One HTTP health-probe outcome. ``ok`` is any 2xx/3xx response."""

    ok: bool
    status_code: int | None
    detail: str


class ComposeDeployer(Protocol):
    """Brings a workspace's Compose stack up/down and pumps its logs to the UI.
    The tool layer implements this over a DinD sidecar; tests fake it."""

    async def up(self, compose_file: str, *, build: bool) -> ComposeUpResult: ...

    async def down(self, compose_file: str) -> None: ...

    async def stream_logs(self, compose_file: str) -> None: ...


class Prober(Protocol):
    """Performs one HTTP GET against a candidate URL and reports reachability."""

    async def probe(self, url: str) -> ProbeResult: ...


# --- DTO ---------------------------------------------------------------------


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID | None
    name: str
    compose_file: str
    #: pending | running | failed | stopped
    status: str
    url: str | None
    port: int | None
    services: list[str]
    #: unknown | green | red
    health: str
    detail: str
    created_at: datetime
    updated_at: datetime


# --- orchestration -----------------------------------------------------------


def _select_endpoint(endpoints: dict[str, str]) -> tuple[int, str] | None:
    """Pick the primary reachable endpoint deterministically: the lowest
    published app port (so a multi-service stack always resolves to the same
    front-door URL across re-deploys)."""
    ports = [int(p) for p in endpoints if str(p).isdigit()]
    if not ports:
        return None
    port = min(ports)
    return port, endpoints[str(port)]


async def _probe_until_healthy(
    prober: Prober, url: str, *, attempts: int, delay_s: float
) -> ProbeResult:
    last = ProbeResult(ok=False, status_code=None, detail="not probed")
    total = max(1, attempts)
    for attempt in range(total):
        last = await prober.probe(url)
        if last.ok:
            return last
        if attempt + 1 < total:
            await asyncio.sleep(delay_s)
    return last


async def deploy_stack(
    db: AsyncEngine,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID | None,
    name: str,
    compose_file: str,
    deployer: ComposeDeployer,
    prober: Prober,
    build: bool = True,
    probe_attempts: int = _DEFAULT_PROBE_ATTEMPTS,
    probe_delay_s: float = _DEFAULT_PROBE_DELAY_S,
) -> DeploymentOut:
    """Deploy (or re-deploy) ``name``'s Compose stack locally and record it.

    Re-deploying the same app reuses its record (idempotent on ``project_id,
    name``): the row is reset to ``pending`` first, so a re-deploy that fails
    never leaves the previous run's reachable URL advertised.
    """
    async with db.begin() as conn:
        row = await repo.upsert_pending(
            conn, project_id, task_id, name=name, compose_file=compose_file
        )
    deployment_id: uuid.UUID = row.id

    try:
        up = await deployer.up(compose_file, build=build)
    except Exception as exc:
        async with db.begin() as conn:
            await repo.mark_failed(conn, deployment_id, detail=f"compose up failed: {exc}")
        raise

    selected = _select_endpoint(up.endpoints)
    if selected is None:
        async with db.begin() as conn:
            row = await repo.mark_result(
                conn,
                deployment_id,
                status="failed",
                url=None,
                port=None,
                services=up.services,
                health="red",
                detail="stack came up but published no host-reachable port",
            )
        return DeploymentOut.model_validate(row)

    port, url = selected
    probe = await _probe_until_healthy(prober, url, attempts=probe_attempts, delay_s=probe_delay_s)
    healthy = probe.ok
    async with db.begin() as conn:
        # An unhealthy stack came up but does not respond — never advertise its URL
        # as reachable; record it failed with the reason (and no URL/port).
        row = await repo.mark_result(
            conn,
            deployment_id,
            status="running" if healthy else "failed",
            url=url if healthy else None,
            port=port if healthy else None,
            services=up.services,
            health="green" if healthy else "red",
            detail="" if healthy else f"health check failed: {probe.detail}",
        )
    if healthy:
        # Pump the running stack's container logs to the dashboard Logs view over
        # the same tool.output path — best-effort; a logging hiccup never fails a
        # healthy deploy.
        with contextlib.suppress(Exception):
            await deployer.stream_logs(compose_file)
    return DeploymentOut.model_validate(row)


async def teardown_stack(
    db: AsyncEngine,
    *,
    project_id: uuid.UUID,
    name: str,
    deployer: ComposeDeployer,
) -> DeploymentOut | None:
    """Tear ``name``'s stack down and mark the deployment stopped. Returns
    ``None`` when no deployment was ever recorded for it (a no-op teardown)."""
    async with db.begin() as conn:
        row = await repo.get_by_name(conn, project_id, name)
    if row is None:
        return None
    await deployer.down(row.compose_file)
    async with db.begin() as conn:
        row = await repo.mark_stopped(conn, row.id)
    return DeploymentOut.model_validate(row)


# --- reads (router) ----------------------------------------------------------


async def get_deployment(conn: AsyncConnection, deployment_id: uuid.UUID) -> DeploymentOut:
    row = await repo.get(conn, deployment_id)
    if row is None:
        raise not_found("deployment")
    return DeploymentOut.model_validate(row)


async def list_deployments(conn: AsyncConnection, project_id: uuid.UUID) -> list[DeploymentOut]:
    rows = await repo.list_for_project(conn, project_id)
    return [DeploymentOut.model_validate(row) for row in rows]
