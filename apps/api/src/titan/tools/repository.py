"""tool_audit persistence: hash-chained append + chain verification.

Appends serialize per project via a transaction-scoped advisory lock, so
``prev_hash`` linkage is race-free under concurrent tool calls. Verification
(TITAN-INT-024 acceptance) recomputes every row hash from stored columns and
walks the linkage; a mutated historical row breaks at its own seq.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from titan.db import uuid7
from titan.gateway.rbac import granting_permissions

GENESIS_HASH = "0" * 64

AuditStatus = str  # 'started' | 'ok' | 'error'


def canonical_hash(payload: Any) -> str:
    """sha256 over a canonical JSON rendering (sorted keys, tight separators,
    non-JSON scalars stringified) — stable across processes."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_hash(fields: dict[str, Any], prev_hash: str) -> str:
    return canonical_hash({**fields, "prev_hash": prev_hash})


def _pg_safe(value: Any) -> Any:
    """Replace NUL characters (which PostgreSQL jsonb rejects) with the Unicode
    replacement character, recursively, in both keys and values."""
    if isinstance(value, str):
        return value.replace(chr(0), chr(0xFFFD))
    if isinstance(value, dict):
        return {_pg_safe(key): _pg_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_pg_safe(item) for item in value]
    return value


def _hashed_fields(
    *,
    tool_call_id: str,
    trace_id: str,
    project_id: uuid.UUID,
    task_id: uuid.UUID | None,
    agent_id: str,
    tool: str,
    args_hash: str,
    args_redacted: dict[str, Any],
    status: str,
    error_code: str | None,
    duration_ms: int | None,
    result_hash: str | None,
    initiator_chain: list[str],
) -> dict[str, Any]:
    # args_redacted is hashed as a NESTED hash so the row-hash preimage stays
    # flat; it is the only stored copy of the arguments (raw args are never
    # persisted), so it MUST sit inside the integrity envelope — otherwise the
    # readable audit record could be rewritten without breaking the chain.
    return {
        "tool_call_id": tool_call_id,
        "trace_id": trace_id,
        "project_id": str(project_id),
        "task_id": str(task_id) if task_id is not None else None,
        "agent_id": agent_id,
        "tool": tool,
        "args_hash": args_hash,
        "args_redacted_hash": canonical_hash(args_redacted),
        "status": status,
        "error_code": error_code,
        "duration_ms": duration_ms,
        "result_hash": result_hash,
        "initiator_chain": initiator_chain,
    }


@dataclass(frozen=True)
class AppendedAudit:
    id: uuid.UUID
    row_hash: str


async def append_audit(
    conn: AsyncConnection,
    *,
    tool_call_id: str,
    trace_id: str,
    project_id: uuid.UUID,
    task_id: uuid.UUID | None,
    agent_id: str,
    tool: str,
    args_hash: str,
    args_redacted: dict[str, Any],
    status: AuditStatus,
    initiator_chain: list[str],
    error_code: str | None = None,
    duration_ms: int | None = None,
    result_hash: str | None = None,
) -> AppendedAudit:
    """Append one chained row. Must run inside a transaction (the per-project
    advisory lock is transaction-scoped and releases at commit)."""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:chain, 0))"),
        {"chain": f"tool_audit:{project_id}"},
    )
    prev = await conn.execute(
        text("SELECT row_hash FROM tool_audit WHERE project_id = :p ORDER BY seq DESC LIMIT 1"),
        {"p": project_id},
    )
    prev_hash = prev.scalar_one_or_none() or GENESIS_HASH
    # Canonicalize ONCE through a JSON round-trip so the value that gets hashed
    # is byte-identical to what verify_chain later reads back from JSONB.
    # NUL sanitization: PostgreSQL jsonb rejects , and hostile fuzz args
    # must still be auditable — the ledger write may never fail on input shape.
    stored_args: dict[str, Any] = _pg_safe(json.loads(json.dumps(args_redacted, default=str)))
    fields = _hashed_fields(
        tool_call_id=tool_call_id,
        trace_id=trace_id,
        project_id=project_id,
        task_id=task_id,
        agent_id=agent_id,
        tool=tool,
        args_hash=args_hash,
        args_redacted=stored_args,
        status=status,
        error_code=error_code,
        duration_ms=duration_ms,
        result_hash=result_hash,
        initiator_chain=initiator_chain,
    )
    row_hash = _row_hash(fields, prev_hash)
    audit_id = uuid7()
    params = {key: value for key, value in fields.items() if key != "args_redacted_hash"}
    await conn.execute(
        text(
            "INSERT INTO tool_audit (id, tool_call_id, trace_id, project_id, task_id, agent_id,"
            " tool, args_hash, args_redacted, status, error_code, duration_ms, result_hash,"
            " initiator_chain, prev_hash, row_hash) "
            "VALUES (:id, :tool_call_id, :trace_id, :project_id, :task_id, :agent_id, :tool,"
            " :args_hash, CAST(:args_redacted AS jsonb), :status, :error_code, :duration_ms,"
            " :result_hash, CAST(:initiator_chain AS jsonb), :prev_hash, :row_hash)"
        ),
        {
            "id": audit_id,
            **params,
            "project_id": project_id,
            "task_id": task_id,
            "args_redacted": json.dumps(stored_args),
            "initiator_chain": json.dumps(initiator_chain),
            "prev_hash": prev_hash,
            "row_hash": row_hash,
        },
    )
    return AppendedAudit(id=audit_id, row_hash=row_hash)


async def rows_for_call(conn: AsyncConnection, tool_call_id: str) -> list[Row[Any]]:
    result = await conn.execute(
        text("SELECT * FROM tool_audit WHERE tool_call_id = :c ORDER BY seq"),
        {"c": tool_call_id},
    )
    return list(result)


async def verify_chain(conn: AsyncConnection, project_id: uuid.UUID) -> int | None:
    """Recompute the project's chain; return the seq of the first row whose
    stored hash or linkage no longer matches (None = chain intact)."""
    result = await conn.execute(
        text(
            "SELECT seq, tool_call_id, trace_id, project_id, task_id, agent_id, tool, args_hash,"
            " args_redacted, status, error_code, duration_ms, result_hash, initiator_chain,"
            " prev_hash, row_hash"
            " FROM tool_audit WHERE project_id = :p ORDER BY seq"
        ),
        {"p": project_id},
    )
    expected_prev = GENESIS_HASH
    for row in result:
        fields = _hashed_fields(
            tool_call_id=row.tool_call_id,
            trace_id=row.trace_id,
            project_id=row.project_id,
            task_id=row.task_id,
            agent_id=row.agent_id,
            tool=row.tool,
            args_hash=row.args_hash,
            args_redacted=row.args_redacted,
            status=row.status,
            error_code=row.error_code,
            duration_ms=row.duration_ms,
            result_hash=row.result_hash,
            initiator_chain=row.initiator_chain,
        )
        if row.prev_hash != expected_prev or row.row_hash != _row_hash(fields, row.prev_hash):
            return int(row.seq)
        expected_prev = row.row_hash
    return None


async def approvers_for_project(conn: AsyncConnection, project_id: uuid.UUID) -> list[uuid.UUID]:
    """The HUMAN users who can resolve an admin-risk approval in this project —
    the recipients of the §7.1 ``approval`` notification (Vol 7 §4.4).

    Mirrors ``PermissionResolver.project_permissions`` for ``tool_approval:write``
    in reverse: a user qualifies via an org-wide grant (``user_roles.project_id IS
    NULL``) OR a grant on THIS project, exactly the two sources the resolver
    unions. The matching permission strings come from ``granting_permissions`` —
    the same rules the forward check uses — so the wildcard forms (``*``,
    ``tool_approval:*``, ``*:write``) are honoured and cannot drift.

    Per-role matching is equivalent to the resolver's union-then-match because the
    predicate is a plain set intersection: no permission is granted only by a
    COMBINATION of two roles, so a match in any single role is necessary and
    sufficient.

    Only ``users`` rows are returned, which is what makes this human-only:
    an agent principal's authority is the ``operator`` bundle carried in its JWT
    (``exchange_api_key`` mints ``roles=["operator"]``), never a ``user_roles``
    row — agents therefore cannot match this query. The ``operator`` bundle holds
    no ``tool_approval:write`` in any case (Vol 8 §4.2 reserves it for
    Owner/Admin/Maintainer). Inactive users are excluded — they cannot sign in to
    act on the request.

    Reads identity-owned tables directly rather than through a service seam,
    following the established precedent for a narrow notification-recipient
    lookup (``titan.plugins.repository.project_owner`` reads ``projects``).
    """
    result = await conn.execute(
        text(
            "SELECT DISTINCT ur.user_id FROM user_roles ur "
            "JOIN roles r ON r.id = ur.role_id "
            "JOIN users u ON u.id = ur.user_id "
            "WHERE u.is_active AND (ur.project_id IS NULL OR ur.project_id = :p) "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(r.permissions) AS perm "
            "WHERE perm = ANY(:grants))"
        ),
        {"p": project_id, "grants": sorted(granting_permissions("tool_approval:write"))},
    )
    return [uuid.UUID(str(row.user_id)) for row in result]


__all__ = [
    "GENESIS_HASH",
    "AppendedAudit",
    "append_audit",
    "approvers_for_project",
    "canonical_hash",
    "rows_for_call",
    "verify_chain",
]
