"""Private data access for the `handoffs` module (handoff_artifacts +
gate_evaluations). SQLAlchemy Core over an AsyncConnection; only ``service.py`` is
importable across the module boundary."""

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7

_ARTIFACT_COLS = (
    "id, project_id, stage, artifact_type, schema_version, version, body, submitted_by, created_at"
)
_GATE_COLS = (
    "id, project_id, stage, artifact_id, attempt, verdict, reasons, evaluator_agent, "
    "evaluator_model, prompt_version, created_at"
)


async def next_artifact_version(conn: AsyncConnection, project_id: uuid.UUID, stage: str) -> int:
    """The next monotonic per-(project, stage) artifact version (max + 1)."""
    result = await conn.execute(
        text(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM handoff_artifacts "
            "WHERE project_id = :pid AND stage = :stage"
        ),
        {"pid": project_id, "stage": stage},
    )
    return int(result.scalar_one())


async def insert_artifact(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    stage: str,
    artifact_type: str,
    schema_version: int,
    version: int,
    body: dict[str, Any],
    submitted_by: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            "INSERT INTO handoff_artifacts "
            "(id, project_id, stage, artifact_type, schema_version, version, body, submitted_by) "
            "VALUES (:id, :pid, :stage, :atype, :sv, :version, CAST(:body AS jsonb), :by) "
            f"RETURNING {_ARTIFACT_COLS}"
        ),
        {
            "id": uuid7(),
            "pid": project_id,
            "stage": stage,
            "atype": artifact_type,
            "sv": schema_version,
            "version": version,
            "body": json.dumps(body),
            "by": submitted_by,
        },
    )
    return result.one()


async def latest_artifact(
    conn: AsyncConnection, project_id: uuid.UUID, stage: str
) -> Row[Any] | None:
    result = await conn.execute(
        text(
            f"SELECT {_ARTIFACT_COLS} FROM handoff_artifacts "
            "WHERE project_id = :pid AND stage = :stage ORDER BY version DESC LIMIT 1"
        ),
        {"pid": project_id, "stage": stage},
    )
    return result.one_or_none()


async def insert_gate_evaluation(
    conn: AsyncConnection,
    *,
    project_id: uuid.UUID,
    stage: str,
    artifact_id: uuid.UUID | None,
    attempt: int,
    verdict: str,
    reasons: list[str],
    evaluator_agent: str,
    evaluator_model: str,
    prompt_version: str,
) -> Row[Any]:
    result = await conn.execute(
        text(
            "INSERT INTO gate_evaluations "
            "(id, project_id, stage, artifact_id, attempt, verdict, reasons, evaluator_agent, "
            "evaluator_model, prompt_version) VALUES (:id, :pid, :stage, :aid, :attempt, :verdict, "
            "CAST(:reasons AS jsonb), :agent, :model, :pv) "
            f"RETURNING {_GATE_COLS}"
        ),
        {
            "id": uuid7(),
            "pid": project_id,
            "stage": stage,
            "aid": artifact_id,
            "attempt": attempt,
            "verdict": verdict,
            "reasons": json.dumps(reasons),
            "agent": evaluator_agent,
            "model": evaluator_model,
            "pv": prompt_version,
        },
    )
    return result.one()


async def failed_gate_count(conn: AsyncConnection, project_id: uuid.UUID, stage: str) -> int:
    """How many times ``stage``'s gate has FAILED for this project — the retry
    counter behind the TITAN-WFL-005 budget."""
    result = await conn.execute(
        text(
            "SELECT count(*) FROM gate_evaluations "
            "WHERE project_id = :pid AND stage = :stage AND verdict = 'fail'"
        ),
        {"pid": project_id, "stage": stage},
    )
    return int(result.scalar_one())


async def gate_evaluations(conn: AsyncConnection, project_id: uuid.UUID) -> Sequence[Row[Any]]:
    """The project's full gate ledger in insert order (the exportable pass/fail
    record for any release, TITAN-WFL-004)."""
    result = await conn.execute(
        text(f"SELECT {_GATE_COLS} FROM gate_evaluations WHERE project_id = :pid ORDER BY id ASC"),
        {"pid": project_id},
    )
    return result.all()


async def gate_evaluations_for_stage(
    conn: AsyncConnection, project_id: uuid.UUID, stage: str
) -> Sequence[Row[Any]]:
    """Every evaluation of one stage's gate — the escalation report's attempt log."""
    result = await conn.execute(
        text(
            f"SELECT {_GATE_COLS} FROM gate_evaluations "
            "WHERE project_id = :pid AND stage = :stage ORDER BY id ASC"
        ),
        {"pid": project_id, "stage": stage},
    )
    return result.all()
