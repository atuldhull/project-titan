"""Public service interface of the ``artifacts`` module (Vol 6 §5, TITAN-WFL-020):
the six auto-generated project artifacts as deterministic, versioned, exportable
views over the domain state.

Only this interface may be imported by other modules. Each artifact is gathered from
existing domain service readers (never a second source of truth), rendered
deterministically (:mod:`titan.artifacts.render`), hashed, and versioned only when
its content actually changes — so a periodic/triggered regeneration is idempotent and
deleting every row then regenerating reproduces byte-identical content (the replay
DoD)."""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.artifacts import render
from titan.artifacts import repository as repo
from titan.clarification import service as clarification
from titan.debt import service as debt
from titan.deploy import service as deploy
from titan.gateway.problems import ProblemException, problem_type
from titan.handoffs import service as handoffs
from titan.orchestrator import service as orchestrator
from titan.planning import service as planning

# The §5 regeneration triggers: which artifact kinds each domain event affects.
# The reconciler regenerates all kinds (idempotent); this map documents the WFL-020
# trigger conditions and is available for event-driven regeneration.
TRIGGERS: dict[str, tuple[str, ...]] = {
    "planning.milestone": ("roadmap", "release_plan", "risk_analysis"),
    "planning.ceremony": ("roadmap", "risk_analysis", "debt_report"),
    "planning.blocker": ("risk_analysis",),
    "task.kanban": ("kanban", "dependency_graph", "roadmap"),
    "handoff.evaluated": ("release_plan", "debt_report", "risk_analysis"),
    "deploy.updated": ("release_plan",),
    "clarification.updated": ("risk_analysis",),
}

# §2.5 physical status → Kanban lane (mirrors orchestrator.transitions._WFL_LANE; the
# board reader groups by raw status, the §5 Kanban artifact groups by lane).
_STATUS_LANE: dict[str, str] = {
    "backlog": "BACKLOG",
    "ready": "READY",
    "blocked": "BLOCKED",
    "assigned": "IN_PROGRESS",
    "in_progress": "IN_PROGRESS",
    "in_review": "IN_LOOP",
    "changes_requested": "IN_LOOP",
    "in_loop": "IN_LOOP",
    "merged": "MERGED",
    "done": "DONE",
    "reopened": "REOPENED",
    "failed": "FAILED",
    "escalated": "ESCALATED",
    "cancelled": "CANCELLED",
}


class DerivedArtifactOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str
    artifact_version: int
    content_md: str
    content_json: dict[str, Any]
    content_hash: str
    created_at: datetime


class ArtifactSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str
    artifact_version: int
    content_hash: str


def _out(row: Row[Any]) -> DerivedArtifactOut:
    return DerivedArtifactOut(
        kind=row.kind,
        artifact_version=row.artifact_version,
        content_md=row.content_md,
        content_json=dict(row.content_json or {}),
        content_hash=row.content_hash,
        created_at=row.created_at,
    )


def _canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(content_md: str, content_json: dict[str, Any]) -> str:
    return hashlib.sha256(
        (content_md + "\x00" + _canonical(content_json)).encode("utf-8")
    ).hexdigest()


async def _latest_body(
    conn: AsyncConnection, project_id: uuid.UUID, stage: str
) -> dict[str, Any] | None:
    """The latest handoff artifact body for a stage, or None if not yet submitted."""
    try:
        artifact = await handoffs.latest_artifact(conn, project_id, stage)
    except ProblemException:
        return None
    return dict(artifact.body or {})


# --- gather (read the domain state for each artifact) ------------------------


async def _gather_roadmap(conn: AsyncConnection, project_id: uuid.UUID) -> dict[str, Any]:
    milestone = await planning.get_milestone(conn, project_id)
    plan = await _latest_body(conn, project_id, "pmgr_plan")
    brief = await _latest_body(conn, project_id, "ceo_vision")
    sprints = [
        {
            "number": int(s["number"]),
            "goal": str(s.get("goal", "")),
            "walking_skeleton": bool(s.get("walking_skeleton", False)),
            "task_ids": [str(t) for t in s.get("task_ids", [])],
        }
        for s in (plan or {}).get("sprints", [])
    ]
    priorities = [
        {
            "feature_id": str(f.get("id")),
            "name": str(f.get("name", "")),
            "moscow": str(f.get("moscow", "wont")),
        }
        for f in (brief or {}).get("features", [])
    ]
    return {
        "current_milestone": milestone.current_milestone,
        "sprints": sprints,
        "priorities": priorities,
    }


async def _gather_kanban(conn: AsyncConnection, project_id: uuid.UUID) -> dict[str, Any]:
    board = await orchestrator.board_for_project(conn, project_id)
    cards = [
        {
            "id": str(card.id),
            "title": card.title,
            "priority": card.priority,
            "lane": _STATUS_LANE.get(column.status, "BACKLOG"),
        }
        for column in board.columns
        for card in column.tasks
    ]
    return {"cards": cards}


async def _gather_dependency_graph(conn: AsyncConnection, project_id: uuid.UUID) -> dict[str, Any]:
    graph = await orchestrator.graph_for_project(conn, project_id)
    arch = await _latest_body(conn, project_id, "architect_design")
    components = (arch or {}).get("components", [])
    comp_nodes = [
        {"name": str(c.get("name")), "layer": str(c.get("layer", ""))} for c in components
    ]
    comp_edges = [
        {"from": str(dep), "to": str(c.get("name"))}
        for c in components
        for dep in c.get("depends_on", [])
    ]
    return {
        "task_nodes": [
            {"id": str(n.id), "title": n.title, "status": n.status} for n in graph.nodes
        ],
        "task_edges": [
            {"task_id": str(e.task_id), "depends_on_id": str(e.depends_on_id)} for e in graph.edges
        ],
        "component_nodes": comp_nodes,
        "component_edges": comp_edges,
    }


async def _gather_release_plan(conn: AsyncConnection, project_id: uuid.UUID) -> dict[str, Any]:
    milestone = await planning.get_milestone(conn, project_id)
    deployments = await deploy.list_deployments(conn, project_id)
    trace = await handoffs.traceability(conn, project_id)
    return {
        "current_milestone": milestone.current_milestone,
        "deployments": [
            {"name": d.name, "status": d.status, "health": d.health, "url": d.url}
            for d in deployments
        ],
        "traceability": trace.model_dump(),
    }


async def _gather_risk_analysis(conn: AsyncConnection, project_id: uuid.UUID) -> dict[str, Any]:
    risks: list[dict[str, Any]] = []

    # Pipeline escalations (needs_human) — deduped by stage.
    try:
        pipeline = await orchestrator.get_pipeline(conn, project_id)
        escalated_stages = sorted({e.stage for e in pipeline.history if e.event == "needs_human"})
    except ProblemException:
        escalated_stages = []
    for stage in escalated_stages:
        risks.append(
            {
                "category": "pipeline_escalation",
                "description": f"Stage '{stage}' escalated to human review",
                "likelihood": 4,
                "impact": 5,
                "owner": "architect",
                "mitigation": "Human arbitration required before the stage can advance",
            }
        )

    # Blocker history (open/escalated blocks).
    for block in await planning.list_blocks(conn, project_id):
        if block.status == "recovered":
            continue
        risks.append(
            {
                "category": "blocker",
                "description": f"Task {block.task_id} blocked: {block.reason}",
                "likelihood": 3,
                "impact": 4,
                "owner": "project_manager",
                "mitigation": f"Recovery ladder at rung '{block.rung}'",
            }
        )

    # Security mediums (accepted-risk debt) + the assumption log.
    ledger = await debt.ledger(conn, project_id)
    for ticket in ledger.tickets:
        if ticket.source == "security" and ticket.status == "open":
            risks.append(
                {
                    "category": "security",
                    "description": f"Accepted medium security finding: {ticket.title}",
                    "likelihood": 3,
                    "impact": 3,
                    "owner": "security",
                    "mitigation": ticket.paydown_proposal or "Schedule a paydown task",
                }
            )
    for statement in await clarification.assumptions_for_release(conn, project_id):
        risks.append(
            {
                "category": "assumption",
                "description": f"Best-judgment assumption: {statement}",
                "likelihood": 2,
                "impact": 3,
                "owner": "ceo",
                "mitigation": "Restated at release approval for user confirmation",
            }
        )
    return {"risks": risks}


async def _gather_debt_report(conn: AsyncConnection, project_id: uuid.UUID) -> dict[str, Any]:
    ledger = await debt.ledger(conn, project_id)
    return {
        "open_total": ledger.open_total,
        "tickets": [
            {
                "severity": t.severity,
                "source": t.source,
                "title": t.title,
                "detail": t.detail,
                "paydown_proposal": t.paydown_proposal,
                "status": t.status,
            }
            for t in ledger.tickets
        ],
    }


_GATHERERS = {
    "roadmap": _gather_roadmap,
    "kanban": _gather_kanban,
    "dependency_graph": _gather_dependency_graph,
    "release_plan": _gather_release_plan,
    "risk_analysis": _gather_risk_analysis,
    "debt_report": _gather_debt_report,
}


# --- regenerate + read -------------------------------------------------------


def _validate_kind(kind: str) -> None:
    if kind not in render.RENDERERS:
        raise ProblemException(
            status=404,
            title="Unknown artifact",
            detail=f"'{kind}' is not one of {list(render.RENDERERS)}",
            problem_type=problem_type("not-found"),
        )


async def regenerate_artifact(
    engine: AsyncEngine, project_id: uuid.UUID, kind: str
) -> DerivedArtifactOut:
    """Gather → render → hash a single artifact, appending a new version only when
    its content changed (idempotent). One transaction: the gather reads and the
    version insert are consistent."""
    _validate_kind(kind)
    async with engine.begin() as conn:
        # Serialize regenerations of this (project, kind) so a concurrent reconciler +
        # API call cannot race the MAX+1 version into a duplicate-key crash.
        await repo.acquire_kind_lock(conn, project_id, kind)
        data = await _GATHERERS[kind](conn, project_id)
        content_md, content_json = render.RENDERERS[kind](data)
        digest = _hash(content_md, content_json)
        cur = await repo.current(conn, project_id, kind)
        if cur is not None and cur.content_hash == digest:
            return _out(cur)  # unchanged → no new version
        row = await repo.insert_version(
            conn,
            project_id,
            kind=kind,
            content_md=content_md,
            content_json=content_json,
            content_hash=digest,
        )
    return _out(row)


async def regenerate_all(engine: AsyncEngine, project_id: uuid.UUID) -> list[DerivedArtifactOut]:
    """Regenerate all six artifacts (each versioned only if it changed)."""
    return [await regenerate_artifact(engine, project_id, kind) for kind in render.ARTIFACT_KINDS]


async def regenerate_for_event(
    engine: AsyncEngine, project_id: uuid.UUID, event_type: str
) -> list[DerivedArtifactOut]:
    """Regenerate exactly the artifact kinds a domain event affects (the WFL-020
    trigger conditions). Unknown event types regenerate nothing."""
    return [
        await regenerate_artifact(engine, project_id, kind) for kind in TRIGGERS.get(event_type, ())
    ]


async def reconcile_all_projects(engine: AsyncEngine) -> int:
    """Regenerate every artifact for every project that has entered its pipeline.
    Idempotent (version-on-change); returns the number of projects reconciled."""
    async with engine.connect() as conn:
        project_ids = await repo.active_project_ids(conn)
    for project_id in project_ids:
        await regenerate_all(engine, project_id)
    return len(project_ids)


async def get_artifact(
    conn: AsyncConnection, project_id: uuid.UUID, kind: str
) -> DerivedArtifactOut:
    """The current version of one artifact (404 if never generated)."""
    _validate_kind(kind)
    row = await repo.current(conn, project_id, kind)
    if row is None:
        raise ProblemException(
            status=404,
            title="Artifact not generated",
            detail=f"'{kind}' has not been generated for this project yet",
            problem_type=problem_type("not-found"),
        )
    return _out(row)


async def list_artifacts(conn: AsyncConnection, project_id: uuid.UUID) -> list[ArtifactSummary]:
    """The current version summary of every generated artifact kind."""
    return [
        ArtifactSummary(
            kind=r.kind, artifact_version=r.artifact_version, content_hash=r.content_hash
        )
        for r in await repo.list_current(conn, project_id)
    ]


async def reset_project_artifacts(engine: AsyncEngine, project_id: uuid.UUID) -> int:
    """Delete every artifact row for a project (the replay-DoD reset)."""
    async with engine.begin() as conn:
        return await repo.delete_for_project(conn, project_id)


__all__ = [
    "ARTIFACT_KINDS",
    "TRIGGERS",
    "ArtifactSummary",
    "DerivedArtifactOut",
    "get_artifact",
    "list_artifacts",
    "reconcile_all_projects",
    "regenerate_all",
    "regenerate_artifact",
    "regenerate_for_event",
    "reset_project_artifacts",
]

ARTIFACT_KINDS = render.ARTIFACT_KINDS
