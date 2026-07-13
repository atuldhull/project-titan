"""The G10 (H10) release-deploy executor — a REAL local deploy + soak, and the
§2.3 G10 failure path.

The DevOps stage does not just *assert* a DeploymentRecord; it actually brings the
generated app up locally (Phase-48 ``deploy.compose`` over an injected
``ComposeDeployer``/``Prober``), health-checks it through the soak window, and:

- on green health → returns a healthy outcome the DevOps agent records as its
  DeploymentRecord (which the pure G10 gate then validates);
- on failure → performs the G10 failure path: **roll back** to the rollback pointer
  (compose down), emit a **``deploy.incident``** domain event, open a **defect task**
  that re-enters engineering (H6 scope), and report the rollback.

Kept out of the pure gate (which stays side-effect-free): this is the executor that
produces the gate's inputs and runs the failure side effects."""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from titan.deploy import service as deploy
from titan.deploy.service import ComposeDeployer, DeploymentOut, Prober
from titan.events.service import STREAM_EVENTS_DOMAIN, write_event
from titan.orchestrator import service as orchestrator

# A soak window: default 15 min at 5s spacing (~180 probes). Tests shorten both.
_DEFAULT_SOAK_ATTEMPTS = 180
_DEFAULT_SOAK_DELAY_S = 5.0


@dataclass(frozen=True)
class DeployGateOutcome:
    """The result of running the real G10 deploy gate."""

    healthy: bool
    deployment: DeploymentOut
    rolled_back: bool
    incident_emitted: bool
    #: The engineering defect task opened on failure (the H6 re-entry), else None.
    defect_task_id: uuid.UUID | None


async def run_deploy_gate(
    engine: AsyncEngine,
    *,
    project_id: uuid.UUID,
    deploy_name: str,
    compose_file: str,
    deployer: ComposeDeployer,
    prober: Prober,
    actor: str,
    soak_attempts: int = _DEFAULT_SOAK_ATTEMPTS,
    soak_delay_s: float = _DEFAULT_SOAK_DELAY_S,
    build: bool = True,
) -> DeployGateOutcome:
    """Deploy the app locally, soak-health-check it, and on failure run the G10
    failure path (rollback + incident + defect task at H6 scope)."""
    deployment = await deploy.deploy_stack(
        engine,
        project_id=project_id,
        task_id=None,
        name=deploy_name,
        compose_file=compose_file,
        deployer=deployer,
        prober=prober,
        build=build,
        probe_attempts=soak_attempts,
        probe_delay_s=soak_delay_s,
    )
    if deployment.health == "green":
        return DeployGateOutcome(
            healthy=True,
            deployment=deployment,
            rolled_back=False,
            incident_emitted=False,
            defect_task_id=None,
        )

    # G10 failure path: roll back to the rollback pointer (compose down), emit an
    # incident, and open a defect task that re-enters engineering (H6 scope).
    stopped = await deploy.teardown_stack(
        engine, project_id=project_id, name=deploy_name, deployer=deployer
    )
    async with engine.begin() as conn:
        # The incident carries the FAILED record (health='red' + the failure detail),
        # not the post-rollback 'stopped' record whose health/detail are cleared — so
        # a consumer sees why the deploy failed.
        await write_event(
            conn,
            STREAM_EVENTS_DOMAIN,
            {"type": "deploy.incident", "deployment": deployment.model_dump(mode="json")},
            actor=actor,
        )
        defect = await orchestrator.create_task(
            conn,
            project_id,
            orchestrator.TaskCreate(
                title=f"G10 deploy failed for '{deploy_name}' — roll-forward fix (H6 re-entry)",
                spec=(
                    f"The local deployment of '{deploy_name}' failed its health check and was "
                    f"rolled back. Detail: {deployment.detail or 'health check red'}."
                ),
                priority=1,
            ),
            actor=actor,
        )
    return DeployGateOutcome(
        healthy=False,
        # The final (rolled-back) record for the caller; the incident above kept the
        # failed record's red health + reason.
        deployment=stopped or deployment,
        rolled_back=True,
        incident_emitted=True,
        defect_task_id=defect.id,
    )


__all__ = ["DeployGateOutcome", "run_deploy_gate"]
