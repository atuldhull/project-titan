"""Cycle-prevention tests (TITAN-BCK-009): explicit A→B→C→A + random DAGs."""

import asyncio
import uuid
from collections.abc import Iterable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import run_async
from titan.db import uuid7
from titan.orchestrator.errors import (
    PROBLEM_TYPE_INVALID_DEPENDENCY_CYCLE,
    DependencyCycleError,
)
from titan.orchestrator.service import add_task_dependency

pytestmark = pytest.mark.schema

N_NODES = 6


def _reachable(edges: set[tuple[int, int]], start: int, goal: int) -> bool:
    """Python-side oracle: is `goal` reachable from `start` over depends_on edges?"""
    frontier, seen = [start], set()
    while frontier:
        node = frontier.pop()
        if node == goal:
            return True
        if node in seen:
            continue
        seen.add(node)
        frontier.extend(dst for (src, dst) in edges if src == node)
    return False


async def _make_project_with_tasks(
    engine: AsyncEngine, count: int
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    user_id, project_id = uuid7(), uuid7()
    task_ids = [uuid7() for _ in range(count)]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, auth_provider, auth_subject) "
                "VALUES (:id, :email, 'Cycle Test', 'local', :subj)"
            ),
            {"id": user_id, "email": f"cycle-{user_id}@titan.local", "subj": str(user_id)},
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, name, display_name, brief, created_by) "
                "VALUES (:id, :name, 'Cycle Test', 'cycle test', :uid)"
            ),
            {"id": project_id, "name": f"cycle-{project_id}", "uid": user_id},
        )
        for i, task_id in enumerate(task_ids):
            await conn.execute(
                text("INSERT INTO tasks (id, project_id, title) VALUES (:id, :pid, :title)"),
                {"id": task_id, "pid": project_id, "title": f"task-{i}"},
            )
    return project_id, task_ids


async def _try_edge(engine: AsyncEngine, task: uuid.UUID, depends_on: uuid.UUID) -> bool:
    """Attempt an edge in its own transaction; True = accepted."""
    try:
        async with engine.begin() as conn:
            await add_task_dependency(conn, task, depends_on)
        return True
    except DependencyCycleError:
        return False


def test_explicit_a_b_c_a_cycle_rejected(migrated_engine: AsyncEngine) -> None:
    async def scenario() -> None:
        _, (a, b, c, *_) = await _make_project_with_tasks(migrated_engine, 3)
        async with migrated_engine.begin() as conn:
            await add_task_dependency(conn, a, b)
        async with migrated_engine.begin() as conn:
            await add_task_dependency(conn, b, c)
        with pytest.raises(DependencyCycleError) as excinfo:
            async with migrated_engine.begin() as conn:
                await add_task_dependency(conn, c, a)
        assert excinfo.value.problem_type == PROBLEM_TYPE_INVALID_DEPENDENCY_CYCLE
        assert excinfo.value.status_code == 409

        # The rejected edge must have rolled back with its transaction.
        async with migrated_engine.connect() as conn:
            count = await conn.execute(
                text(
                    "SELECT count(*) FROM task_dependencies "
                    "WHERE task_id = :c AND depends_on_id = :a"
                ),
                {"c": c, "a": a},
            )
            assert count.scalar() == 0

    run_async(scenario)


def test_self_dependency_rejected(migrated_engine: AsyncEngine) -> None:
    async def scenario() -> None:
        _, (a, *_) = await _make_project_with_tasks(migrated_engine, 1)
        with pytest.raises(DependencyCycleError):
            async with migrated_engine.begin() as conn:
                await add_task_dependency(conn, a, a)

    run_async(scenario)


def test_concurrent_opposite_edges_cannot_commit_a_cycle(
    migrated_engine: AsyncEngine,
) -> None:
    """The advisory lock closes the READ COMMITTED race: T1 inserts A→B and
    holds its transaction open; T2 attempts B→A meanwhile. T2 must block on
    the graph lock until T1 commits, then see T1's edge and reject."""

    async def scenario() -> None:
        _, (a, b, *_) = await _make_project_with_tasks(migrated_engine, 2)
        t1_inserted = asyncio.Event()
        t2_attempting = asyncio.Event()

        async def t1() -> None:
            async with migrated_engine.begin() as conn:
                await add_task_dependency(conn, a, b)
                t1_inserted.set()
                await t2_attempting.wait()
                # Hold the transaction (and the lock) while T2 is blocked.
                await asyncio.sleep(0.3)

        async def t2() -> bool:
            await t1_inserted.wait()
            t2_attempting.set()
            return await _try_edge(migrated_engine, b, a)

        _, t2_accepted = await asyncio.gather(t1(), t2())
        assert t2_accepted is False, "concurrent opposite edge committed a cycle"

        async with migrated_engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT count(*) FROM task_dependencies "
                    "WHERE (task_id = :a AND depends_on_id = :b) "
                    "   OR (task_id = :b AND depends_on_id = :a)"
                ),
                {"a": a, "b": b},
            )
            assert rows.scalar() == 1  # only T1's edge survived

    run_async(scenario)


@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    edges=st.lists(
        st.tuples(st.integers(0, N_NODES - 1), st.integers(0, N_NODES - 1)).filter(
            lambda e: e[0] != e[1]
        ),
        max_size=14,
    )
)
def test_random_dags_match_reachability_oracle(
    migrated_engine: AsyncEngine, edges: Iterable[tuple[int, int]]
) -> None:
    """Every attempted edge is accepted iff the Python oracle says it is acyclic."""

    async def scenario() -> None:
        _, tasks = await _make_project_with_tasks(migrated_engine, N_NODES)
        accepted: set[tuple[int, int]] = set()
        for src, dst in edges:
            expect_ok = not _reachable(accepted, start=dst, goal=src)
            actually_ok = await _try_edge(migrated_engine, tasks[src], tasks[dst])
            assert actually_ok == expect_ok, (
                f"edge {src}->{dst}: db={actually_ok} oracle={expect_ok} "
                f"accepted so far={sorted(accepted)}"
            )
            if actually_ok:
                accepted.add((src, dst))

    run_async(scenario)
