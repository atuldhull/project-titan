"""Public service interface of the `orchestrator` module (TITAN-BCK-004).

Responsibility: task state machine, assignment, prioritization, scheduling.
Owns: tasks, task_dependencies.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.orchestrator.errors import DependencyCycleError

__all__ = ["DependencyCycleError", "add_task_dependency"]

# Transaction-scoped advisory lock serializing ALL dependency-graph writes.
# Without it, two READ COMMITTED transactions inserting A→B and B→A each see
# a snapshot without the other's edge and both commit — a persistent cycle.
# Graph writes are rare planning operations; a single global lock is cheap.
# (Global, not per-project: the schema permits cross-project edges.)
_DEP_GRAPH_LOCK_KEY = 0x7469_7461_6E64_6570  # "titandep"

_ACQUIRE_GRAPH_LOCK = text("SELECT pg_advisory_xact_lock(:key)")

_INSERT_EDGE = text(
    """
    INSERT INTO task_dependencies (task_id, depends_on_id)
    VALUES (:task_id, :depends_on_id)
    ON CONFLICT (task_id, depends_on_id) DO NOTHING
    """
)

# Reachability over depends_on edges, starting from the new edge's target:
# if task_id is reachable from depends_on_id, the new edge closed a cycle.
_CYCLE_CHECK = text(
    """
    WITH RECURSIVE reach(node) AS (
        SELECT depends_on_id FROM task_dependencies WHERE task_id = :depends_on_id
        UNION
        SELECT td.depends_on_id
        FROM task_dependencies td
        JOIN reach r ON td.task_id = r.node
    )
    SELECT 1 FROM reach WHERE node = :task_id LIMIT 1
    """
)


async def add_task_dependency(
    conn: AsyncConnection, task_id: uuid.UUID, depends_on_id: uuid.UUID
) -> None:
    """Insert a dependency edge, rejecting cycles (TITAN-BCK-009).

    Graph writes serialize on a transaction-scoped advisory lock, so the
    recursive-CTE reachability check always sees every committed edge —
    safe against concurrent writers even at READ COMMITTED. A raised
    :class:`DependencyCycleError` rolls the edge back with the enclosing
    transaction (the lock releases with it, commit or rollback).
    """
    if task_id == depends_on_id:
        raise DependencyCycleError(task_id, depends_on_id)

    await conn.execute(_ACQUIRE_GRAPH_LOCK, {"key": _DEP_GRAPH_LOCK_KEY})
    await conn.execute(_INSERT_EDGE, {"task_id": task_id, "depends_on_id": depends_on_id})
    cycle = await conn.execute(_CYCLE_CHECK, {"task_id": task_id, "depends_on_id": depends_on_id})
    if cycle.scalar() is not None:
        raise DependencyCycleError(task_id, depends_on_id)
