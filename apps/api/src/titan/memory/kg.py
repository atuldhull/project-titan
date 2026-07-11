"""Knowledge-graph internals (Vol 5 §7, ADR-05-04, TITAN-BRN-013/014) — private
to the `memory` module.

The graph is relational (``brain.kg_nodes`` / ``brain.kg_edges``): typed nodes,
typed edges with ``valid_from``/``valid_to`` history, recursive-CTE traversal
hard-capped at depth 3. This module provides:

- the §7.3-item-1 STRUCTURED WRITERS (deterministic, no LLM, callable inside a
  workflow transaction): an approved ADR → ``decision`` node + DECIDED_FOR /
  CONSTRAINS edges; a scaffolded service → ``component`` node + PART_OF; a filed
  lesson → ``lesson`` node + DERIVED_FROM; superseding a decision stamps
  ``valid_to`` and adds a SUPERSEDES edge (history kept, never deleted);
- KB provenance writes on approval (``kb_doc`` node + AUTHORED_BY + DERIVED_FROM
  lesson) for §10.3;
- typed traversal for ``POST /memory/kg/query``;
- :class:`GraphKgSink`, the real implementation of the §13-step-11 KG hook that
  Phase 38 shipped as a typed no-op.

Entity resolution by embedding similarity (TITAN-BRN-014, cosine ≥ 0.92 via
``titan_kg_labels``) is explicitly OUT of MVP scope for Phase 39 — only the
deterministic exact-name dedup (the UNIQUE (scope, node_type, name) key) is
built here; the fuzzy merge is deferred and recorded in the requirements
checklist.
"""

import json
import uuid
from collections.abc import Sequence
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.memory import repository as repo

logger = structlog.get_logger("titan.memory.kg")

# §7.1 — the ten typed node families and ten typed edge families (mirrors the
# migration's CHECK vocabularies; a bad type is a clean 422 at the boundary).
NODE_TYPES = (
    "project",
    "component",
    "decision",
    "pattern",
    "technology",
    "agent",
    "person",
    "incident",
    "lesson",
    "kb_doc",
)
EDGE_TYPES = (
    "PART_OF",
    "DEPENDS_ON",
    "DECIDED_FOR",
    "SUPERSEDES",
    "APPLIES",
    "DERIVED_FROM",
    "CONSTRAINS",
    "AUTHORED_BY",
    "USES",
    "DOCUMENTED_IN",
)

MAX_TRAVERSAL_DEPTH = 3  # §7.1: recursive CTEs capped at depth 3


def parse_kg_scope(scope: str) -> uuid.UUID | None:
    """'org' | 'project:<uuid>' → the project id (or None for org). Local copy of
    the memory-service scope rule to keep ``kg`` free of a service import cycle."""
    if scope == "org":
        return None
    if scope.startswith("project:"):
        try:
            return uuid.UUID(scope[len("project:") :])
        except ValueError as exc:
            raise ValueError("project scope must be 'project:<uuid>'") from exc
    raise ValueError("scope must be 'org' or 'project:<uuid>'")


def allowed_scopes(scope: str) -> tuple[str, ...]:
    """The ONLY scopes a traversal may touch (TITAN-BRN-003): org + the caller's
    own project — never another project."""
    return ("org",) if scope == "org" else ("org", scope)


# --- DTOs --------------------------------------------------------------------


class KgNodeRef(BaseModel):
    """Name a start node by its natural key (scope defaults to the query scope)."""

    model_config = ConfigDict(extra="forbid")

    node_type: str
    name: str = Field(min_length=1)

    @field_validator("node_type")
    @classmethod
    def _validate_node_type(cls, value: str) -> str:
        if value not in NODE_TYPES:
            raise ValueError(f"node_type must be one of {NODE_TYPES}")
        return value


class KgNodeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    node_type: str
    scope: str
    name: str
    properties: dict[str, Any]
    depth: int


class KgEdgeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    src: uuid.UUID
    dst: uuid.UUID
    edge_type: str
    properties: dict[str, Any]


class KgQueryRequest(BaseModel):
    """POST /memory/kg/query — typed traversal (start nodes, edge-type filter,
    depth ≤ 3)."""

    model_config = ConfigDict(extra="forbid")

    scope: str = Field(min_length=1)
    start: list[KgNodeRef] = Field(default_factory=list)
    start_ids: list[uuid.UUID] = Field(default_factory=list)
    edge_types: list[str] = Field(default_factory=list)
    direction: str = "both"
    max_depth: int = Field(default=MAX_TRAVERSAL_DEPTH, ge=0, le=MAX_TRAVERSAL_DEPTH)

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        parse_kg_scope(value)
        return value

    @field_validator("edge_types")
    @classmethod
    def _validate_edge_types(cls, value: list[str]) -> list[str]:
        unknown = [edge for edge in value if edge not in EDGE_TYPES]
        if unknown:
            raise ValueError(f"edge_types must be a subset of {EDGE_TYPES}; got {unknown}")
        return value

    @field_validator("direction")
    @classmethod
    def _validate_direction(cls, value: str) -> str:
        if value not in ("out", "in", "both"):
            raise ValueError("direction must be 'out', 'in', or 'both'")
        return value


class KgQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[KgNodeOut]
    edges: list[KgEdgeOut]


# --- structured writers (§7.3 item 1, TITAN-BRN-013) --------------------------
# Deterministic, no LLM, run inside the CALLER's workflow transaction so the
# node/edge writes commit atomically with the event that produced them.


def _props(properties: dict[str, Any] | None) -> str:
    return json.dumps(properties or {}, ensure_ascii=False)


async def ensure_node(
    conn: AsyncConnection,
    *,
    scope: str,
    node_type: str,
    name: str,
    properties: dict[str, Any] | None = None,
    record_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Idempotent node upsert (exact-name dedup; TITAN-BRN-014's deterministic
    half). Re-imports merge properties rather than duplicate the node."""
    if node_type not in NODE_TYPES:
        raise ValueError(f"node_type must be one of {NODE_TYPES}")
    return await repo.ensure_kg_node(
        conn,
        node_type=node_type,
        scope=scope,
        name=name,
        properties=_props(properties),
        record_id=record_id,
    )


async def add_edge(
    conn: AsyncConnection,
    *,
    src: uuid.UUID,
    dst: uuid.UUID,
    edge_type: str,
    properties: dict[str, Any] | None = None,
) -> uuid.UUID | None:
    """Add a current edge, idempotently (no-op if an active same-type edge
    exists). Returns the new edge id, or None when one already existed."""
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"edge_type must be one of {EDGE_TYPES}")
    return await repo.add_kg_edge(
        conn, src=src, dst=dst, edge_type=edge_type, properties=_props(properties)
    )


async def record_decision(
    conn: AsyncConnection,
    *,
    scope: str,
    name: str,
    decided_for: list[KgNodeRef] | None = None,
    constrains: list[KgNodeRef] | None = None,
    properties: dict[str, Any] | None = None,
    record_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """An approved ADR → a ``decision`` node + DECIDED_FOR edges to the
    project/component(s) it was decided for + CONSTRAINS edges to the
    component(s) it constrains (§7.3 item 1)."""
    decision = await ensure_node(
        conn,
        scope=scope,
        node_type="decision",
        name=name,
        properties=properties,
        record_id=record_id,
    )
    for target in decided_for or []:
        dst = await ensure_node(conn, scope=scope, node_type=target.node_type, name=target.name)
        await add_edge(conn, src=decision, dst=dst, edge_type="DECIDED_FOR")
    for target in constrains or []:
        dst = await ensure_node(conn, scope=scope, node_type=target.node_type, name=target.name)
        await add_edge(conn, src=decision, dst=dst, edge_type="CONSTRAINS")
    return decision


async def record_component(
    conn: AsyncConnection,
    *,
    scope: str,
    name: str,
    part_of: KgNodeRef | None = None,
    properties: dict[str, Any] | None = None,
    record_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A scaffolded service/module → a ``component`` node + a PART_OF edge to its
    parent (project or containing component)."""
    component = await ensure_node(
        conn,
        scope=scope,
        node_type="component",
        name=name,
        properties=properties,
        record_id=record_id,
    )
    if part_of is not None:
        parent = await ensure_node(
            conn, scope=scope, node_type=part_of.node_type, name=part_of.name
        )
        await add_edge(conn, src=component, dst=parent, edge_type="PART_OF")
    return component


async def record_lesson(
    conn: AsyncConnection,
    *,
    scope: str,
    name: str,
    derived_from: KgNodeRef | None = None,
    properties: dict[str, Any] | None = None,
    record_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A filed retrospective → a ``lesson`` node + a DERIVED_FROM edge to the
    incident/context it was learned from."""
    lesson = await ensure_node(
        conn,
        scope=scope,
        node_type="lesson",
        name=name,
        properties=properties,
        record_id=record_id,
    )
    if derived_from is not None:
        source = await ensure_node(
            conn, scope=scope, node_type=derived_from.node_type, name=derived_from.name
        )
        await add_edge(conn, src=lesson, dst=source, edge_type="DERIVED_FROM")
    return lesson


async def supersede_decision(
    conn: AsyncConnection,
    *,
    scope: str,
    superseded_name: str,
    superseding_name: str,
    properties: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Replace one decision with another: stamp ``valid_to`` on the superseded
    decision's current out-edges (history kept, not deleted) and add a
    SUPERSEDES edge from the new decision to the old one (§7.3)."""
    old = await ensure_node(conn, scope=scope, node_type="decision", name=superseded_name)
    new = await ensure_node(
        conn, scope=scope, node_type="decision", name=superseding_name, properties=properties
    )
    await repo.expire_kg_edges_from(conn, src=old)
    await add_edge(conn, src=new, dst=old, edge_type="SUPERSEDES")
    return new


async def write_kb_provenance(
    conn: AsyncConnection,
    *,
    scope: str,
    slug: str,
    title: str,
    approved_by: str,
    derived_from_lesson: str | None = None,
    record_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """KB approval provenance (§10.3): a ``kb_doc`` node (name = slug), an
    AUTHORED_BY edge to the approver (a ``person`` for a human id, an ``agent``
    for an ``agent.*`` id), and a DERIVED_FROM edge to a lesson when the proposal
    was distilled from one."""
    kb_doc = await ensure_node(
        conn,
        scope=scope,
        node_type="kb_doc",
        name=slug,
        properties={"title": title},
        record_id=record_id,
    )
    author_type = "agent" if approved_by.startswith("agent.") else "person"
    author = await ensure_node(conn, scope=scope, node_type=author_type, name=approved_by)
    await add_edge(conn, src=kb_doc, dst=author, edge_type="AUTHORED_BY")
    if derived_from_lesson:
        lesson = await ensure_node(conn, scope=scope, node_type="lesson", name=derived_from_lesson)
        await add_edge(conn, src=kb_doc, dst=lesson, edge_type="DERIVED_FROM")
    return kb_doc


# --- traversal (§7.1, POST /memory/kg/query) ---------------------------------


async def traverse(conn: AsyncConnection, request: KgQueryRequest) -> KgQueryResult:
    """Resolve the start set (by id and/or natural-key ref), then walk the graph
    within the caller's scope up to ``max_depth`` (≤3). Start nodes outside the
    allowed scopes, or unknown refs, are simply absent from the walk."""
    scopes = allowed_scopes(request.scope)
    start_ids: list[uuid.UUID] = list(request.start_ids)
    for ref in request.start:
        node_id = await repo.get_kg_node_id(
            conn, scope=request.scope, node_type=ref.node_type, name=ref.name
        )
        if node_id is not None:
            start_ids.append(node_id)
    nodes, edges = await repo.traverse_kg(
        conn,
        start_ids=start_ids,
        edge_types=request.edge_types or None,
        scopes=scopes,
        max_depth=request.max_depth,
        direction=request.direction,
    )
    return KgQueryResult(
        nodes=[
            KgNodeOut(
                id=row.id,
                node_type=row.node_type,
                scope=row.scope,
                name=row.name,
                properties=_as_dict(row.properties),
                depth=int(row.depth),
            )
            for row in nodes
        ],
        edges=[
            KgEdgeOut(
                id=row.id,
                src=row.src,
                dst=row.dst,
                edge_type=row.edge_type,
                properties=_as_dict(row.properties),
            )
            for row in edges
        ],
    )


def _as_dict(value: Any) -> dict[str, Any]:
    """asyncpg returns jsonb as str or dict depending on codec — normalise."""
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return value if isinstance(value, dict) else {}


# --- the §13-step-11 KG hook (fills Phase 38's typed no-op) -------------------


class GraphKgSink:
    """The real KG sink: translates the runtime's per-turn KG edge events into
    deterministic, idempotent graph writes (§13 step 11). Best-effort — a graph
    write must never fail the turn whose messages already committed, so the whole
    batch runs in one guarded transaction and logs metadata only (TITAN-OPS-016).

    Events are duck-typed (``kind``/``label``/``project_id``/``task_id``/
    ``source_agent``) so ``memory`` need not import ``runtime`` (the independence
    contract). Each event ensures the project node and links a typed node to it:
    decision → DECIDED_FOR, component → PART_OF, lesson → DERIVED_FROM."""

    _EDGE_FOR_KIND = {
        "decision": "DECIDED_FOR",
        "component": "PART_OF",
        "lesson": "DERIVED_FROM",
    }

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def emit(self, events: Sequence[Any]) -> None:
        if not events:
            return
        try:
            async with self._engine.begin() as conn:
                for event in events:
                    edge_type = self._EDGE_FOR_KIND.get(event.kind)
                    if edge_type is None:
                        continue
                    scope = f"project:{event.project_id}"
                    project = await ensure_node(
                        conn, scope=scope, node_type="project", name=str(event.project_id)
                    )
                    name = f"{event.label}:{event.task_id}" if event.task_id else event.label
                    node = await ensure_node(
                        conn,
                        scope=scope,
                        node_type=event.kind,
                        name=name,
                        properties={"source_agent": event.source_agent},
                    )
                    await add_edge(conn, src=node, dst=project, edge_type=edge_type)
        except Exception as exc:
            logger.warning("memory.kg_sink_write_failed", error=type(exc).__name__)


__all__ = [
    "EDGE_TYPES",
    "MAX_TRAVERSAL_DEPTH",
    "NODE_TYPES",
    "GraphKgSink",
    "KgEdgeOut",
    "KgNodeOut",
    "KgNodeRef",
    "KgQueryRequest",
    "KgQueryResult",
    "add_edge",
    "allowed_scopes",
    "ensure_node",
    "parse_kg_scope",
    "record_component",
    "record_decision",
    "record_lesson",
    "supersede_decision",
    "traverse",
    "write_kb_provenance",
]
