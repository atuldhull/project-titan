"""Private data access for the `runs` module (runs, artifacts). SQLAlchemy Core
over an AsyncConnection; only ``service.py`` crosses the module boundary."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.gateway.pagination import Keyset

_RUN_COLS = (
    "id, task_id, agent_id, attempt, status, input, output_summary, error, provider, model, "
    "tokens_in, tokens_out, cost_usd, started_at, finished_at, created_at, version"
)
_ARTIFACT_COLS = (
    "id, run_id, project_id, kind, path, storage_ref, sha256, size_bytes, created_at, version"
)


async def lock_task_runs(conn: AsyncConnection, task_id: uuid.UUID) -> None:
    """Serialize run creation for one task on a transaction-scoped advisory lock
    so ``MAX(attempt)+1`` is race-safe under READ COMMITTED (the same technique
    the dependency-graph writes use). Releases with the enclosing transaction."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"run-attempt:{task_id}"},
    )


async def next_attempt(conn: AsyncConnection, task_id: uuid.UUID) -> int:
    result = await conn.execute(
        text("SELECT COALESCE(MAX(attempt), 0) + 1 FROM runs WHERE task_id = :tid"),
        {"tid": task_id},
    )
    return int(result.scalar_one())


async def insert_run(
    conn: AsyncConnection,
    *,
    run_id: uuid.UUID,
    task_id: uuid.UUID,
    agent_id: uuid.UUID,
    attempt: int,
    run_input: str,
    provider: str | None,
    model: str | None,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO runs (id, task_id, agent_id, attempt, input, provider, model) "
            f"VALUES (:id, :task_id, :agent_id, :attempt, CAST(:input AS jsonb), "
            f":provider, :model) RETURNING {_RUN_COLS}"
        ),
        {
            "id": run_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "attempt": attempt,
            "input": run_input,
            "provider": provider,
            "model": model,
        },
    )
    return result.one()


async def get_run(conn: AsyncConnection, run_id: uuid.UUID) -> Row[Any] | None:
    result = await conn.execute(
        text(f"SELECT {_RUN_COLS} FROM runs WHERE id = :id"), {"id": run_id}
    )
    return result.one_or_none()


async def list_runs_by_task(
    conn: AsyncConnection, *, task_id: uuid.UUID, keyset: Keyset
) -> Sequence[Row[Any]]:
    seek, seek_params = keyset.where()
    extra = f"AND {seek}" if seek else ""
    result = await conn.execute(
        text(
            f"SELECT {_RUN_COLS} FROM runs WHERE task_id = :tid {extra} "
            f"ORDER BY {keyset.order_by} LIMIT :limit"
        ),
        {"tid": task_id, "limit": keyset.fetch, **seek_params},
    )
    return result.all()


async def cancel_run(
    conn: AsyncConnection, run_id: uuid.UUID, *, allowed_from: Sequence[str]
) -> Row[Any] | None:
    """queued|running → cancelled (stamps finished_at); None if missing or in a
    terminal status (the caller maps that to 404 vs 409)."""
    result = await conn.execute(
        text(
            f"UPDATE runs SET status = 'cancelled', finished_at = now(), version = version + 1 "
            f"WHERE id = :id AND status = ANY(:allowed) RETURNING {_RUN_COLS}"
        ),
        {"id": run_id, "allowed": list(allowed_from)},
    )
    return result.one_or_none()


# --- worker run lifecycle (§8.2) -------------------------------------------
# All guarded on the current status so re-delivery/reclaim is idempotent: a run
# only advances from a non-terminal state, so at most one worker records its
# result/artifacts (TITAN-BCK-016 'no duplicate side effects').


async def mark_running(conn: AsyncConnection, run_id: uuid.UUID) -> Row[Any] | None:
    """Take (or re-take) a non-terminal run: queued|running|timed_out → running,
    stamping started_at once and clearing any prior finished_at (a re-taken
    timed_out run is no longer finished). Returns None ONLY for a missing or
    terminal (succeeded|failed|cancelled) run — the idempotency gate that lets a
    reclaimed in-flight/timed-out run retry while a finalized one is skipped."""
    result = await conn.execute(
        text(
            f"UPDATE runs SET status = 'running', started_at = COALESCE(started_at, now()), "
            f"finished_at = NULL, version = version + 1 "
            f"WHERE id = :id AND status IN ('queued', 'running', 'timed_out') RETURNING {_RUN_COLS}"
        ),
        {"id": run_id},
    )
    return result.one_or_none()


async def finalize_run(
    conn: AsyncConnection,
    run_id: uuid.UUID,
    *,
    status: str,
    output_summary: str | None,
    error: str | None,
    provider: str | None,
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    cost_usd: object,
) -> Row[Any] | None:
    """queued|running → terminal (succeeded|failed), recording accounting +
    finished_at. Returns None if already finalized (a peer won) — the caller
    then skips artifact/result writes, so there is exactly one finalizer."""
    result = await conn.execute(
        text(
            f"UPDATE runs SET status = :status, output_summary = :output_summary, error = :error, "
            f"provider = :provider, model = :model, tokens_in = :tokens_in, "
            f"tokens_out = :tokens_out, cost_usd = :cost_usd, finished_at = now(), "
            f"version = version + 1 "
            f"WHERE id = :id AND status IN ('queued', 'running') RETURNING {_RUN_COLS}"
        ),
        {
            "id": run_id,
            "status": status,
            "output_summary": output_summary,
            "error": error,
            "provider": provider,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost_usd,
        },
    )
    return result.one_or_none()


async def mark_timed_out(conn: AsyncConnection, run_id: uuid.UUID) -> Row[Any] | None:
    """queued|running → timed_out (stamps finished_at). None if already terminal."""
    result = await conn.execute(
        text(
            f"UPDATE runs SET status = 'timed_out', finished_at = now(), version = version + 1 "
            f"WHERE id = :id AND status IN ('queued', 'running') RETURNING {_RUN_COLS}"
        ),
        {"id": run_id},
    )
    return result.one_or_none()


async def insert_artifact(
    conn: AsyncConnection,
    *,
    artifact_id: uuid.UUID,
    run_id: uuid.UUID,
    project_id: uuid.UUID,
    kind: str,
    path: str | None,
    storage_ref: str,
    sha256: str,
    size_bytes: int,
) -> Row[Any]:
    result = await conn.execute(
        text(
            f"INSERT INTO artifacts (id, run_id, project_id, kind, path, storage_ref, sha256, "
            f"size_bytes) VALUES (:id, :run_id, :project_id, :kind, :path, :storage_ref, :sha256, "
            f":size_bytes) RETURNING {_ARTIFACT_COLS}"
        ),
        {
            "id": artifact_id,
            "run_id": run_id,
            "project_id": project_id,
            "kind": kind,
            "path": path,
            "storage_ref": storage_ref,
            "sha256": sha256,
            "size_bytes": size_bytes,
        },
    )
    return result.one()


async def list_artifacts_by_run(
    conn: AsyncConnection, *, run_id: uuid.UUID, keyset: Keyset
) -> Sequence[Row[Any]]:
    seek, seek_params = keyset.where()
    extra = f"AND {seek}" if seek else ""
    result = await conn.execute(
        text(
            f"SELECT {_ARTIFACT_COLS} FROM artifacts WHERE run_id = :rid {extra} "
            f"ORDER BY {keyset.order_by} LIMIT :limit"
        ),
        {"rid": run_id, "limit": keyset.fetch, **seek_params},
    )
    return result.all()
