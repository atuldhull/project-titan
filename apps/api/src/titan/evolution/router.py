"""Evolution API (Vol 10 §2/§6.1). Mounted by main.py.

Read endpoints ride ``evolution:read`` (a non-sensitive resource, so ``*:read``
reaches it — the dashboard can show which prompt version served a task,
TITAN-EVO-015c). The approval action requires ``evolution:approve`` AND a human
principal (agents may open/benchmark EPs but never approve, TITAN-EVO-001 /
ADR-EVO-01); it records the immutable ledger row + the audit event.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from titan.evolution import service as evolution_service
from titan.evolution.manifest import content_sha256
from titan.evolution.resolver import EvolutionResolver
from titan.gateway.problems import ProblemException
from titan.gateway.rbac import Requires
from titan.identity.service import Principal


def _bad_request(detail: str) -> ProblemException:
    return ProblemException(status=400, title="Bad request", detail=detail)


async def _audit_rcr_update(
    request: Request, principal: Principal, rcr_id: Any, detail: dict[str, Any]
) -> None:
    """Trail a human RCR mutation (link/override) — the merged-EP path auto-remediates
    linked RCRs, so this is trust-relevant and must be audited like an approval."""
    from titan.audit.service import AuditAction, Outcome

    await request.app.state.audit.record(
        actor=f"user:{principal.sub}",
        action=AuditAction.EVOLUTION_RCR_UPDATE,
        resource=f"rcr:{rcr_id}",
        outcome=Outcome.SUCCESS,
        source_ip=request.client.host if request.client else None,
        detail=detail,
    )


router = APIRouter(prefix="/api/v1/evolution", tags=["evolution"])

_Reader = Annotated[Principal, Depends(Requires("evolution:read"))]
_Approver = Annotated[Principal, Depends(Requires("evolution:approve"))]


def _resolver(request: Request) -> EvolutionResolver:
    resolver: EvolutionResolver | None = getattr(request.app.state, "evolution_resolver", None)
    if resolver is None:  # pragma: no cover - misconfiguration
        raise _bad_request("evolution resolver not configured")
    return resolver


class ManifestSummary(BaseModel):
    version: int
    prompt_count: int
    base_sha256: str
    routing_sha256: str


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    path: str = Field(min_length=1)
    content_sha256: str = Field(min_length=64, max_length=64)
    semver: str = "0.0.0"
    ep: str = Field(min_length=1)
    diff_url: str = ""


class LinkEpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ep: str = Field(min_length=1, max_length=200)


class OverrideRcrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failure_code: str | None = Field(default=None, min_length=1, max_length=16)
    severity: str | None = None
    status: str | None = None


_RCR_SEVERITIES = {"low", "medium", "high", "critical"}
_RCR_STATUSES = {"open", "remediated", "accepted_risk"}


class DeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ep: str = Field(min_length=1, max_length=200)
    agent_id: str = Field(min_length=1, max_length=100)
    mode: str = Field(pattern="^(shadow|canary)$")
    champion_ref: str = Field(min_length=1, max_length=200)
    challenger_ref: str = Field(min_length=1, max_length=200)
    kpi_floor: float = Field(ge=0, le=100)
    canary_pct: int = Field(default=0, ge=0, le=100)
    min_samples: int = Field(default=5, ge=1, le=1000)
    observation_window_s: int = Field(default=300, ge=30, le=86400)


class DeployDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)


class DebateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    context: str = Field(min_length=1, max_length=20000)
    decision_class: str = Field(min_length=1, max_length=50)
    project_id: str | None = None
    convening_agent_id: str | None = None


@router.get("/manifest", response_model=ManifestSummary)
async def get_manifest(request: Request, principal: _Reader) -> ManifestSummary:
    m = _resolver(request).manifest
    return ManifestSummary(
        version=m.version,
        prompt_count=len(m.prompts),
        base_sha256=m.base.sha256,
        routing_sha256=m.routing.sha256,
    )


@router.get("/prompts")
async def get_prompt_versions(request: Request, principal: _Reader) -> dict[str, Any]:
    """The pinned prompt SHA + semver + EP per agent (TITAN-EVO-015c)."""
    return {"prompts": evolution_service.active_prompt_versions(_resolver(request))}


@router.get("/artifacts")
async def list_artifacts(
    request: Request,
    principal: _Reader,
    kind: str | None = None,
    ref: str | None = None,
    approved_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    rows = await evolution_service.list_artifacts(
        request.app.state.engine, kind=kind, ref=ref, approved_only=approved_only, limit=limit
    )
    return {"artifacts": rows}


@router.get("/agents/{agent_id}/kpis")
async def get_agent_kpis(request: Request, principal: _Reader, agent_id: str) -> dict[str, Any]:
    """Per-agent, per-task-class KPI aggregates over 7/30/90-day windows
    (TITAN-EVO-006). Reconciles with the raw scorecards (means computed directly)."""
    from titan.evolution import kpi as kpi_mod

    return await kpi_mod.agent_kpis(request.app.state.engine, agent_id)


@router.get("/rcrs")
async def list_rcrs(
    request: Request,
    principal: _Reader,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    """Root-Cause Records (TITAN-EVO-012). Also searchable in the Memory Explorer
    (each RCR has a companion memory_records row, subtype='rcr')."""
    from titan.evolution import repository as repo

    rows = await repo.list_rcrs(request.app.state.engine, status=status, limit=limit)
    return {"rcrs": rows}


@router.patch("/rcrs/{rcr_id}/ep")
async def link_rcr_ep(
    request: Request, principal: _Approver, rcr_id: str, body: LinkEpRequest
) -> dict[str, Any]:
    """Link an RCR to the EP proposed to remediate it (EVO-013). Human-only, like
    approval: asserting "this EP fixes this failure" is a curation act, and the link
    is what lets a merged EP auto-remediate the RCR. Recorded before the EP merges."""
    import uuid as _uuid

    from titan.evolution import repository as repo

    if principal.kind != "user":
        raise ProblemException(
            status=403, title="Forbidden", detail="linking an RCR to an EP is human-only"
        )
    try:
        rid = _uuid.UUID(rcr_id)
    except (ValueError, TypeError) as exc:
        raise _bad_request("rcr_id is not a valid UUID") from exc
    row = await repo.link_rcr_ep(request.app.state.engine, rid, body.ep)
    if row is None:
        raise ProblemException(status=404, title="Not found", detail="RCR not found")
    await _audit_rcr_update(request, principal, rid, {"op": "link_ep", "ep": body.ep})
    return {"linked": True, "rcr": row}


@router.patch("/rcrs/{rcr_id}")
async def override_rcr(
    request: Request, principal: _Approver, rcr_id: str, body: OverrideRcrRequest
) -> dict[str, Any]:
    """Human override of an RCR's classification/severity/status (EVO-011: the Memory
    Agent's automatic code is human-overridable). Human-only. The failure_code is
    validated against the pinned taxonomy so a typo is a clean 400, not a DB 500."""
    import uuid as _uuid

    from titan.evolution import failures
    from titan.evolution import repository as repo

    if principal.kind != "user":
        raise ProblemException(
            status=403, title="Forbidden", detail="overriding an RCR is human-only"
        )
    resolver = _resolver(request)
    if body.failure_code is not None and body.failure_code not in failures.load_taxonomy(
        resolver.repo_root
    ):
        raise _bad_request("failure_code is not in the pinned taxonomy")
    if body.severity is not None and body.severity not in _RCR_SEVERITIES:
        raise _bad_request("severity must be one of low|medium|high|critical")
    if body.status is not None and body.status not in _RCR_STATUSES:
        raise _bad_request("status must be one of open|remediated|accepted_risk")
    try:
        rid = _uuid.UUID(rcr_id)
    except (ValueError, TypeError) as exc:
        raise _bad_request("rcr_id is not a valid UUID") from exc
    row = await repo.override_rcr(
        request.app.state.engine,
        rid,
        failure_code=body.failure_code,
        severity=body.severity,
        status=body.status,
    )
    if row is None:
        raise ProblemException(status=404, title="Not found", detail="RCR not found or no change")
    await _audit_rcr_update(
        request,
        principal,
        rid,
        {
            "op": "override",
            "failure_code": body.failure_code,
            "severity": body.severity,
            "status": body.status,
        },
    )
    return {"updated": True, "rcr": row}


@router.get("/investigations")
async def list_investigations(
    request: Request,
    principal: _Reader,
    status: str = "open",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    """Open (or resolved) KPI-degradation investigations (TITAN-EVO-007)."""
    rows = await evolution_service.list_investigations(
        request.app.state.engine, status=status, limit=limit
    )
    return {"investigations": rows}


@router.get("/deployments")
async def list_deployments(request: Request, principal: _Reader) -> dict[str, Any]:
    """Active A/B challenger deployments under a monitoring contract (EVO-018)."""
    from titan.evolution import rollback

    rows = await rollback.list_active_deployments(request.app.state.engine)
    return {"deployments": rows}


@router.post("/deployments", status_code=201)
async def create_deployment(
    request: Request, principal: _Approver, body: DeployRequest
) -> dict[str, Any]:
    """Deploy a challenger config for online A/B under a KPI-floor monitoring contract
    (EVO-018/003). Human-only: putting a challenger in front of (shadow) real traffic is a
    curation act. Refused if an active deployment already exists for the agent."""
    from titan.evolution import rollback

    if principal.kind != "user":
        raise ProblemException(
            status=403, title="Forbidden", detail="deploying a challenger is human-only"
        )
    row = await rollback.create_deployment(
        request.app.state.engine,
        ep=body.ep,
        agent_id=body.agent_id,
        mode=body.mode,
        champion_ref=body.champion_ref,
        challenger_ref=body.challenger_ref,
        kpi_floor=body.kpi_floor,
        canary_pct=body.canary_pct,
        min_samples=body.min_samples,
        observation_window_s=body.observation_window_s,
    )
    if row is None:
        raise _bad_request("an active deployment already exists for this agent")
    return {"deployment": row}


@router.get("/deployments/{deployment_id}/analysis")
async def deployment_analysis(
    request: Request, principal: _Reader, deployment_id: str
) -> dict[str, Any]:
    """The sequential (SPRT-style) promote/abort/continue recommendation (EVO-018)."""
    import uuid as _uuid

    from titan.evolution import ab

    try:
        did = _uuid.UUID(deployment_id)
    except (ValueError, TypeError) as exc:
        raise _bad_request("deployment_id is not a valid UUID") from exc
    result = await ab.analyze_deployment(request.app.state.engine, did)
    return {"analysis": result.__dict__}


@router.post("/deployments/{deployment_id}:promote")
async def promote_deployment(
    request: Request, principal: _Approver, deployment_id: str, body: DeployDecisionRequest
) -> dict[str, Any]:
    return await _deployment_decision(request, principal, deployment_id, body, "promoted")


@router.post("/deployments/{deployment_id}:rollback")
async def rollback_deployment_endpoint(
    request: Request, principal: _Approver, deployment_id: str, body: DeployDecisionRequest
) -> dict[str, Any]:
    return await _deployment_decision(request, principal, deployment_id, body, "rolled_back")


async def _deployment_decision(
    request: Request,
    principal: Principal,
    deployment_id: str,
    body: DeployDecisionRequest,
    status: str,
) -> dict[str, Any]:
    """Human promote/abort/rollback of a deployment (EVO-018). Human-only + audited."""
    import uuid as _uuid

    from titan.evolution import rollback

    if principal.kind != "user":
        raise ProblemException(
            status=403, title="Forbidden", detail="deciding a deployment is human-only"
        )
    try:
        did = _uuid.UUID(deployment_id)
    except (ValueError, TypeError) as exc:
        raise _bad_request("deployment_id is not a valid UUID") from exc
    dep = await rollback.get_deployment(request.app.state.engine, did)
    if dep is None or dep["status"] not in ("shadow", "canary"):
        raise ProblemException(status=404, title="Not found", detail="no active deployment")
    row = await rollback.set_terminal_status(
        request.app.state.engine,
        request.app.state.audit,
        dep,
        status=status,
        actor=f"user:{principal.sub}",
        reason=body.reason,
    )
    return {"deployment": row}


@router.post("/debate", status_code=201)
async def convene_debate_endpoint(
    request: Request, principal: _Approver, body: DebateRequest
) -> dict[str, Any]:
    """Convene a multi-model debate on a decision (EVO-021/022/023). Human-only. Gated by
    the ``evolution.debate`` flag — with it off this returns 409 (no debate can run)."""
    import uuid as _uuid

    from titan.evolution import debate as debate_mod

    if principal.kind != "user":
        raise ProblemException(
            status=403, title="Forbidden", detail="convening a debate is human-only"
        )
    try:
        project_id = _uuid.UUID(body.project_id) if body.project_id else None
        convening = _uuid.UUID(body.convening_agent_id) if body.convening_agent_id else None
    except (ValueError, TypeError) as exc:
        raise _bad_request("project_id / convening_agent_id must be UUIDs") from exc
    # A named project must exist — else the ADR/transcript writes would fail the FK check
    # and be silently swallowed (a ruling returned but never persisted). Fail loudly instead.
    if project_id is not None:
        from sqlalchemy import text as _text

        async with request.app.state.engine.connect() as conn:
            exists = (
                await conn.execute(
                    _text("SELECT 1 FROM projects WHERE id = :pid"), {"pid": project_id}
                )
            ).first()
        if exists is None:
            raise ProblemException(status=404, title="Not found", detail="project not found")
    policy = debate_mod.load_policy(_resolver(request).repo_root)
    try:
        result = await debate_mod.convene_debate(
            request.app.state.engine,
            request.app.state.llm_gateway,
            request.app.state.flags,
            question=body.question,
            context=body.context,
            decision_class=body.decision_class,
            policy=policy,
            project_id=project_id,
            convening_agent_id=convening,
            subject=f"user:{principal.sub}",
            redis=getattr(request.app.state, "redis", None),
        )
    except debate_mod.DebateDisabled as exc:
        raise ProblemException(status=409, title="Debate disabled", detail=str(exc)) from exc
    except debate_mod.DebateCapExceeded as exc:
        raise ProblemException(status=429, title="Debate cap reached", detail=str(exc)) from exc
    except debate_mod.JudgeNotIndependent as exc:
        raise _bad_request(str(exc)) from exc
    return {"debate": result.__dict__}


@router.post("/artifacts:approve", status_code=201)
async def approve_artifact(
    request: Request, principal: _Approver, body: ApproveRequest
) -> dict[str, Any]:
    """Record a human approval of an artifact version (TITAN-EVO-001c).

    Only a human may approve (ADR-EVO-01): an agent principal — even one somehow
    holding the scope — is refused. The approved bytes must exist on disk and hash
    to ``content_sha256`` so a phantom hash can never be signed off."""
    if principal.kind != "user":
        raise ProblemException(
            status=403, title="Forbidden", detail="evolution:approve is human-only (ADR-EVO-01)"
        )
    resolver = _resolver(request)
    # Only a path the manifest actually declares may be approved: this both scopes
    # the endpoint to the registry (no ../ traversal, no absolute path) and prevents
    # an existence/hash-prefix oracle over arbitrary files the API process can read.
    declared = {e.path for e in resolver.manifest.all_entries()}
    if body.path not in declared:
        raise _bad_request("path is not a declared evolution artifact")
    artifact_path = resolver.repo_root / body.path
    if not artifact_path.exists():
        raise _bad_request("artifact file not found")
    if content_sha256(artifact_path.read_bytes()) != body.content_sha256:
        # Generic message: never echo the on-disk hash (no prefix oracle).
        raise _bad_request("artifact bytes do not match content_sha256")
    row = await evolution_service.approve_artifact(
        request.app.state.engine,
        request.app.state.audit,
        kind=body.kind,
        ref=body.ref,
        path=body.path,
        content_sha256=body.content_sha256,
        semver=body.semver,
        ep=body.ep,
        diff_url=body.diff_url,
        approver_user_id=principal.sub,
        actor=f"user:{principal.sub}",
        source_ip=request.client.host if request.client else None,
    )
    return {"approved": True, "artifact": row}


def mount(app: FastAPI) -> None:
    app.include_router(router)
