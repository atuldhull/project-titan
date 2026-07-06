"""Admin-risk approval records + the DB-backed approval gate (Vol 7 §4.4,
TITAN-INT-022).

Lifecycle: an unapproved admin-risk call creates a ``pending`` record (deduped
per project+tool+args by a partial unique index) and is rejected
PERMISSION_DENIED. A human resolves it via the approve/deny endpoints. The
next call with the SAME project, tool, and argument hash atomically consumes
an ``approved`` record (single-use — a second identical call needs a fresh
approval) and dispatches. A ``denied`` record keeps rejecting without
re-creating pendings, so a looping agent cannot spam the dashboard.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from titan.db import uuid7
from titan.gateway.dto import JsonbDict
from titan.gateway.pagination import Keyset
from titan.gateway.problems import ProblemException, problem_type
from titan.gateway.rbac import not_found
from titan.tools.bus import ApprovalDecision
from titan.tools.envelopes import ToolCallEnvelope
from titan.tools.redaction import Redactor, default_redactor
from titan.tools.registry import ToolSpec
from titan.tools.repository import canonical_hash

ApprovalStatus = Literal["pending", "approved", "denied", "consumed"]

_COLS = (
    "id, project_id, task_id, agent_id, tool, args_hash, args_redacted, status,"
    " resolved_by, resolved_at, created_at, version"
)


class ToolApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID | None
    agent_id: str
    tool: str
    args_hash: str
    args_redacted: JsonbDict
    status: str
    resolved_by: uuid.UUID | None
    resolved_at: datetime | None
    created_at: datetime
    version: int


# --- repository --------------------------------------------------------------


async def _consume_approved(
    conn: AsyncConnection, project_id: uuid.UUID, tool: str, args_hash: str
) -> uuid.UUID | None:
    """Atomically flip one matching ``approved`` record to ``consumed``.
    SKIP LOCKED keeps two concurrent identical calls from double-spending."""
    row = await conn.execute(
        text(
            "UPDATE tool_approvals SET status = 'consumed', version = version + 1 "
            "WHERE id = (SELECT id FROM tool_approvals WHERE project_id = :p AND tool = :t "
            "AND args_hash = :h AND status = 'approved' ORDER BY created_at "
            "LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id"
        ),
        {"p": project_id, "t": tool, "h": args_hash},
    )
    value = row.scalar_one_or_none()
    return uuid.UUID(str(value)) if value is not None else None


async def _find_status(
    conn: AsyncConnection, project_id: uuid.UUID, tool: str, args_hash: str, status: str
) -> uuid.UUID | None:
    row = await conn.execute(
        text(
            "SELECT id FROM tool_approvals WHERE project_id = :p AND tool = :t "
            "AND args_hash = :h AND status = :s ORDER BY created_at DESC LIMIT 1"
        ),
        {"p": project_id, "t": tool, "h": args_hash, "s": status},
    )
    value = row.scalar_one_or_none()
    return uuid.UUID(str(value)) if value is not None else None


async def _create_pending(
    conn: AsyncConnection, call: ToolCallEnvelope, args_hash: str, args_redacted: dict[str, Any]
) -> uuid.UUID | None:
    """Insert a pending record, or reuse the one a concurrent identical call
    won the race to create. Returns None only if that winner was ALSO resolved
    (approved/denied/consumed) before we could read it — the caller re-evaluates
    from the top."""
    inserted = await conn.execute(
        text(
            "INSERT INTO tool_approvals (id, project_id, task_id, agent_id, tool, args_hash,"
            " args_redacted, status) "
            "VALUES (:id, :p, :task, :agent, :tool, :hash, CAST(:args AS jsonb), 'pending') "
            "ON CONFLICT (project_id, tool, args_hash) WHERE status = 'pending' DO NOTHING "
            "RETURNING id"
        ),
        {
            "id": uuid7(),
            "p": call.project_id,
            "task": call.task_id,
            "agent": call.agent_id,
            "tool": call.tool,
            "hash": args_hash,
            "args": json.dumps(args_redacted, default=str),
        },
    )
    value = inserted.scalar_one_or_none()
    if value is not None:
        return uuid.UUID(str(value))
    # Lost the race to a concurrent identical call — reuse its pending record.
    # (May be None if that record was resolved in the same window — not an
    # invariant, so never assert on it.)
    return await _find_status(conn, call.project_id, call.tool, args_hash, "pending")


async def list_approvals(
    conn: AsyncConnection, project_id: uuid.UUID, *, keyset: Keyset, status: str | None
) -> list[Row[Any]]:
    clauses = ["project_id = :p"]
    params: dict[str, Any] = {"p": project_id, "limit": keyset.fetch}
    if status is not None:
        clauses.append("status = :s")
        params["s"] = status
    seek, seek_params = keyset.where()
    if seek:
        clauses.append(seek)
        params.update(seek_params)
    result = await conn.execute(
        text(
            f"SELECT {_COLS} FROM tool_approvals WHERE {' AND '.join(clauses)} "
            f"ORDER BY {keyset.order_by} LIMIT :limit"
        ),
        params,
    )
    return list(result)


# --- human resolution path (approve / deny endpoints) -------------------------


def _conflict(detail: str) -> ProblemException:
    return ProblemException(
        status=409, title="Conflict", detail=detail, problem_type=problem_type("conflict")
    )


async def resolve_approval(
    conn: AsyncConnection,
    *,
    approval_id: uuid.UUID,
    project_id: uuid.UUID,
    action: Literal["approved", "denied"],
    resolved_by: uuid.UUID,
) -> ToolApprovalOut:
    """pending → approved|denied. Missing or cross-project → 404 (no existence
    leak, TITAN-SEC-032); already resolved → 409."""
    row = await conn.execute(
        text(
            f"UPDATE tool_approvals SET status = :new, resolved_by = :u, resolved_at = now(),"
            f" version = version + 1 WHERE id = :id AND project_id = :p AND status = 'pending'"
            f" RETURNING {_COLS}"
        ),
        {"new": action, "u": resolved_by, "id": approval_id, "p": project_id},
    )
    updated = row.first()
    if updated is not None:
        return ToolApprovalOut.model_validate(updated)
    probe = await conn.execute(
        text("SELECT status FROM tool_approvals WHERE id = :id AND project_id = :p"),
        {"id": approval_id, "p": project_id},
    )
    status = probe.scalar_one_or_none()
    if status is None:
        raise not_found("approval")
    raise _conflict(f"Approval is already {status}.")


# --- the §4.4 risk gate (wired as the bus's ApprovalGate) ----------------------


class DbApprovalGate:
    """The production approval gate: consume-approved / reuse-pending /
    respect-denied / create-pending, in one transaction."""

    def __init__(self, engine: AsyncEngine, *, redactor: Redactor = default_redactor) -> None:
        self._engine = engine
        self._redact = redactor

    async def __call__(self, call: ToolCallEnvelope, spec: ToolSpec) -> ApprovalDecision:
        args_hash = canonical_hash(call.arguments)
        redacted = dict(self._redact(call.arguments))
        # Bounded retry: the only way a single pass fails to reach a verdict is a
        # lost create-race whose winner was resolved in the same window, which
        # the next pass observes as consumed/denied. Three passes is ample.
        for _ in range(3):
            async with self._engine.begin() as conn:
                consumed = await _consume_approved(conn, call.project_id, call.tool, args_hash)
                if consumed is not None:
                    return ApprovalDecision(True, f"approval {consumed} consumed")
                pending = await _find_status(conn, call.project_id, call.tool, args_hash, "pending")
                if pending is not None:
                    return ApprovalDecision(
                        False, f"approval request {pending} is awaiting human review"
                    )
                denied = await _find_status(conn, call.project_id, call.tool, args_hash, "denied")
                if denied is not None:
                    return ApprovalDecision(False, f"approval request {denied} was denied")
                created = await _create_pending(conn, call, args_hash, redacted)
                if created is not None:
                    return ApprovalDecision(
                        False, f"approval request {created} created and awaiting human review"
                    )
        # Extremely unlikely: every pass lost its race and saw the winner
        # already resolved. Fail closed rather than dispatch unapproved.
        return ApprovalDecision(False, "approval could not be established; retry")


__all__ = [
    "ApprovalStatus",
    "DbApprovalGate",
    "ToolApprovalOut",
    "list_approvals",
    "resolve_approval",
]
