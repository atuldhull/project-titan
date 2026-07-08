"""Public service interface of the `runs` module (TITAN-BCK-004).

Responsibility: execution records of agent work and artifact metadata.
Owns: runs, artifacts.

Only this interface may be imported by other modules; routers, repositories,
and ORM models stay private to the package. Pydantic v2 DTOs — never
SQLAlchemy models — cross this boundary.
"""

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7
from titan.gateway.dto import JsonbDict
from titan.gateway.pagination import Keyset, Page
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import not_found
from titan.runs import repository as repo
from titan.runs.storage import Storage

RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "timed_out"]
ArtifactKind = Literal["code_patch", "file", "test_report", "doc", "diagram", "build_log"]

# A run may be cancelled only while it has not reached a terminal status (§8.2).
_CANCELLABLE: tuple[str, ...] = ("queued", "running")


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    # Resolved prompt-context refs (§8.2); free-form JSON at MVP.
    input: dict[str, Any] = Field(default_factory=dict)
    provider: str | None = None
    model: str | None = None


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    agent_id: uuid.UUID
    attempt: int
    status: str
    input: JsonbDict
    output_summary: str | None
    error: str | None
    provider: str | None
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: Decimal | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    version: int


class ArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    path: str | None
    storage_ref: str
    sha256: str
    size_bytes: int
    created_at: datetime
    version: int


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type=problem_type("conflict")
    )


# --- runs ------------------------------------------------------------------


async def create_run(conn: AsyncConnection, task_id: uuid.UUID, data: RunCreate) -> RunOut:
    """Create a new queued run for a task (attempt = max+1). A bad agent_id (or
    a vanished task) FK is a 422, not a 500. Per-task creation serializes on an
    advisory lock so concurrent creates can't mint a duplicate attempt (the
    uq_runs_task_id_attempt constraint is the durable backstop). A task with an
    ACTIVE run already has its one owner — a second is a 409, enforced by the
    uq_runs_single_active_owner partial index in the DB itself (TITAN-AGT-012)."""
    await repo.lock_task_runs(conn, task_id)
    attempt = await repo.next_attempt(conn, task_id)
    try:
        row = await repo.insert_run(
            conn,
            run_id=uuid7(),
            task_id=task_id,
            agent_id=data.agent_id,
            attempt=attempt,
            run_input=json.dumps(data.input),
            provider=data.provider,
            model=data.model,
        )
    except IntegrityError as exc:
        if "uq_runs_single_active_owner" in str(exc.orig):
            raise _conflict(
                "Task already has an active run — every task has exactly one "
                "owner agent at any time (TITAN-AGT-012)."
            ) from exc
        raise ProblemException(
            status=422,
            title="Invalid run",
            detail="References an unknown task or agent.",
            problem_type=problem_type("validation"),
        ) from exc
    return RunOut.model_validate(row)


async def get_run(conn: AsyncConnection, run_id: uuid.UUID) -> RunOut:
    row = await repo.get_run(conn, run_id)
    if row is None:
        raise not_found("run")
    return RunOut.model_validate(row)


async def find_run(conn: AsyncConnection, run_id: uuid.UUID) -> RunOut | None:
    """Non-raising lookup for the worker (a vanished run is not a 404)."""
    row = await repo.get_run(conn, run_id)
    return RunOut.model_validate(row) if row is not None else None


async def list_runs(conn: AsyncConnection, *, task_id: uuid.UUID, keyset: Keyset) -> Page[RunOut]:
    rows = await repo.list_runs_by_task(conn, task_id=task_id, keyset=keyset)
    page, cursor = keyset.paginate(rows)
    return Page(items=[RunOut.model_validate(r) for r in page], next_cursor=cursor)


async def cancel_run(conn: AsyncConnection, run_id: uuid.UUID) -> RunOut:
    """queued|running → cancelled; a terminal run is a 409, a missing one 404."""
    row = await repo.cancel_run(conn, run_id, allowed_from=_CANCELLABLE)
    if row is None:
        existing = await repo.get_run(conn, run_id)
        if existing is None:
            raise not_found("run")
        raise _conflict(f"Run is already {existing.status} and cannot be cancelled.")
    return RunOut.model_validate(row)


# --- worker run lifecycle (§8.2); guarded so re-delivery is idempotent ------


async def mark_running(conn: AsyncConnection, run_id: uuid.UUID) -> RunOut | None:
    """Take (or re-take) a non-terminal run: queued|running|timed_out → running
    (started_at stamped once, finished_at cleared). Returns None only for a
    missing or terminal (succeeded|failed|cancelled) run — the idempotency gate
    a reclaimed in-flight/timed-out run passes while a finalized one is skipped."""
    row = await repo.mark_running(conn, run_id)
    return RunOut.model_validate(row) if row is not None else None


async def finalize_run(
    conn: AsyncConnection,
    run_id: uuid.UUID,
    *,
    status: Literal["succeeded", "failed"],
    output_summary: str | None,
    error: str | None,
    provider: str | None,
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    cost_usd: Decimal,
) -> RunOut | None:
    """queued|running → succeeded|failed with accounting. Returns None if a peer
    already finalized it — the caller then skips artifacts/result (one finalizer)."""
    row = await repo.finalize_run(
        conn,
        run_id,
        status=status,
        output_summary=output_summary,
        error=error,
        provider=provider,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
    )
    return RunOut.model_validate(row) if row is not None else None


async def mark_timed_out(conn: AsyncConnection, run_id: uuid.UUID) -> RunOut | None:
    """queued|running → timed_out; None if already terminal."""
    row = await repo.mark_timed_out(conn, run_id)
    return RunOut.model_validate(row) if row is not None else None


# --- artifacts -------------------------------------------------------------


async def record_artifact(
    conn: AsyncConnection,
    storage: Storage,
    *,
    run_id: uuid.UUID,
    project_id: uuid.UUID,
    kind: ArtifactKind,
    data: bytes,
    path: str | None = None,
) -> ArtifactOut:
    """Persist artifact bytes through the Storage protocol and record its
    metadata row. The bytes live behind an opaque storage_ref — the seam the
    worker (Vol 3 §8.2) uses; no raw filesystem path ever reaches a handler."""
    sha256 = hashlib.sha256(data).hexdigest()
    storage_ref = await storage.put(f"runs/{run_id}/{sha256}", data)
    row = await repo.insert_artifact(
        conn,
        artifact_id=uuid7(),
        run_id=run_id,
        project_id=project_id,
        kind=kind,
        path=path,
        storage_ref=storage_ref,
        sha256=sha256,
        size_bytes=len(data),
    )
    return ArtifactOut.model_validate(row)


async def list_artifacts(
    conn: AsyncConnection, *, run_id: uuid.UUID, keyset: Keyset
) -> Page[ArtifactOut]:
    rows = await repo.list_artifacts_by_run(conn, run_id=run_id, keyset=keyset)
    page, cursor = keyset.paginate(rows)
    return Page(items=[ArtifactOut.model_validate(r) for r in page], next_cursor=cursor)


__all__ = [
    "ArtifactKind",
    "ArtifactOut",
    "RunCreate",
    "RunOut",
    "RunStatus",
    "cancel_run",
    "create_run",
    "finalize_run",
    "find_run",
    "get_run",
    "list_artifacts",
    "list_runs",
    "mark_running",
    "mark_timed_out",
    "record_artifact",
]
