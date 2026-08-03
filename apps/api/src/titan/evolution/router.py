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
